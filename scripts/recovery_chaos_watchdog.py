"""Run independently in a Kubernetes Job; never requires the host harness."""

import json
import sys
import time


def healthy(deployment, replicas):
    spec, status = deployment.spec, deployment.status
    return (
        spec.replicas == replicas
        and (status.observed_generation or 0) >= deployment.metadata.generation
        and (status.replicas or 0) == replicas
        and (status.updated_replicas or 0) == replicas
        and (status.ready_replicas or 0) == replicas
        and (status.available_replicas or 0) == replicas
    )


def watch(apps, settings, clock=time.monotonic, sleep=time.sleep, emit=print):
    namespace, worker = settings["namespace"], settings["worker"]
    replicas = settings["replicas"]
    read = lambda: apps.read_namespaced_deployment(worker, namespace)
    def signal():
        annotations = read().metadata.annotations or {}
        if annotations.get("omnivec.io/pr183-chaos-run") != settings["run_id"]:
            raise RuntimeError("Watchdog ownership mismatch")
        return annotations.get("omnivec.io/pr183-chaos-phase")
    # Dry-run verifies admission and RBAC before announcing that protection is ready.
    apps.patch_namespaced_deployment(worker, namespace, {"spec": {"replicas": replicas}}, dry_run="All")
    signal()
    emit("WATCHDOG_READY", flush=True)
    outage_deadline = clock() + settings["outage_seconds"]
    deadline = clock() + settings["arm_timeout"]
    armed = False
    while clock() < deadline:
        try:
            phase = signal()
            if phase == "restored" and healthy(read(), replicas):
                emit("WATCHDOG_DISARMED", flush=True)
                return
            if phase == "armed":
                armed = True
                break
        except Exception:
            # Loss of the coordinator/API must not prevent eventual restoration.
            pass
        sleep(2)
    # An arm write may have committed while its response was lost. Restore even
    # on arm timeout, rather than incorrectly assuming no outage occurred.
    deadline = outage_deadline if armed else clock()
    while clock() < deadline:
        try:
            if signal() == "restored" and healthy(read(), replicas):
                emit("WATCHDOG_DISARMED", flush=True)
                return
        except Exception:
            pass
        sleep(2)
    emit("WATCHDOG_RESTORE_ATTEMPT", flush=True)
    deadline = clock() + settings["restore_seconds"]
    while clock() < deadline:
        try:
            apps.patch_namespaced_deployment(worker, namespace, {"spec": {"replicas": replicas}})
            if healthy(read(), replicas):
                emit("WATCHDOG_RESTORED", flush=True)
                return
        except Exception:
            pass
        sleep(5)
    raise RuntimeError("Watchdog restoration unverified; retain run lock")


def main():
    from kubernetes import client, config

    config.load_incluster_config()
    configuration = client.Configuration.get_default_copy()
    configuration.retries = 0
    api_client = client.ApiClient(configuration)

    # Every Kubernetes call is bounded, including a partitioned control plane.
    class Bounded:
        def __init__(self, api):
            self.api = api

        def __getattr__(self, name):
            return lambda *args, **kwargs: getattr(self.api, name)(*args, _request_timeout=(5, 10), **kwargs)

    try:
        watch(Bounded(client.AppsV1Api(api_client)), json.loads(sys.argv[1]))
    except Exception as error:
        print("WATCHDOG_ERROR=" + type(error).__name__, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
