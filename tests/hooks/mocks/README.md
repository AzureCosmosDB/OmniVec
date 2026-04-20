# Recording mocks for cloud CLIs

Drop-in POSIX shell stubs for `azd`, `az`, `kubectl`, `helm` that let us
run the real `hooks/preprovision.sh` and `hooks/postprovision.sh`
end-to-end **without** a real Azure subscription.

## How it works

Every invocation of the stubs (e.g. `azd env set FOO bar`) appends a
single line to the file named by `$OMNIVEC_MOCK_LOG`:

```
azd env set FOO bar
az provider show --namespace Microsoft.ContainerService --query registrationState -o tsv
kubectl get pods -n omnivec
```

The stubs return canned output that is "good enough" for the hook to
progress (e.g. `az account show` returns a fake subscription id, every
provider is `Registered`, `azd env get-value` returns what was most
recently `azd env set`).

## Usage (POSIX test)

```sh
export OMNIVEC_MOCK_LOG="$(mktemp)"
export PATH="$REPO_ROOT/tests/hooks/mocks:$PATH"
export AZURE_ENV_NAME=mock-env
export OMNIVEC_NONINTERACTIVE=1
export OMNIVEC_FORCE_NO_TTY=1

sh hooks/preprovision.sh </dev/null

grep 'azd env set OMNIVEC_SYSTEM_NODE_VM_SIZE' "$OMNIVEC_MOCK_LOG"
```

## Why no LocalStack-for-Azure?

There is no production-quality emulator for ARM + AKS + Cosmos + Key
Vault + Workload Identity. Azurite/Cosmos emulator/Service Bus emulator
only cover data planes. This harness tests the *hook logic* — that we
call the right azd/az/kubectl/helm commands in the right order with the
right args — which is where most `azd up` regressions actually live.

For what-if validation against real ARM, see `tests/infra/what-if.sh`
(follow-up).
