"""Complete every message in an Azure Service Bus subscription DLQ."""

import asyncio
import os
import sys
import uuid

from azure.identity.aio import DefaultAzureCredential
from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue
from azure.servicebus.exceptions import MessageSizeExceededError
from azure.servicebus.aio import ServiceBusClient


NAMESPACE = os.environ.get("SERVICEBUS_NAMESPACE") or sys.exit(
    "SERVICEBUS_NAMESPACE is required"
)
TOPIC = os.environ.get("SERVICEBUS_TOPIC", "embeddings")
SUBSCRIPTION = os.environ.get("SERVICEBUS_SUBSCRIPTION", "worker")
ACTION = os.environ.get("ACTION", "purge").lower()


async def main():
    if ACTION not in {"purge", "requeue"}:
        sys.exit("ACTION must be 'purge' or 'requeue'")

    credential = DefaultAzureCredential()
    removed = 0
    async with ServiceBusClient(NAMESPACE, credential) as client:
        sender = client.get_topic_sender(TOPIC) if ACTION == "requeue" else None
        async with sender if sender is not None else _null_async_context():
            receiver = client.get_subscription_receiver(
                topic_name=TOPIC,
                subscription_name=SUBSCRIPTION,
                sub_queue=ServiceBusSubQueue.DEAD_LETTER,
                max_wait_time=5,
            )
            async with receiver:
                while True:
                    messages = await receiver.receive_messages(
                        max_message_count=100,
                        max_wait_time=5,
                    )
                    if not messages:
                        break
                    if sender is not None:
                        batch = await sender.create_message_batch()
                        for message in messages:
                            replay = ServiceBusMessage(
                                b"".join(message.body),
                                message_id=f"{message.message_id}-replay-{uuid.uuid4().hex[:8]}",
                                content_type=message.content_type,
                                subject=message.subject,
                                correlation_id=message.correlation_id,
                                application_properties=message.application_properties,
                            )
                            try:
                                batch.add_message(replay)
                            except MessageSizeExceededError:
                                await sender.send_messages(batch)
                                batch = await sender.create_message_batch()
                                batch.add_message(replay)
                        if len(batch):
                            await sender.send_messages(batch)
                    for message in messages:
                        await receiver.complete_message(message)
                        removed += 1
                    print(f"{'requeued' if sender is not None else 'removed'}={removed}")

    await credential.close()
    print(f"done={removed} action={ACTION}")


class _null_async_context:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        return None


if __name__ == "__main__":
    asyncio.run(main())
