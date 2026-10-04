"""Two add_episode_task calls for one group, made back to back, start two workers that run
episodes for that group at the same time. Graphiti requires episodes of a group to be added
sequentially (Graphiti.add_episode docstring). Uses mcp_server/src/services/queue_service.py as is.

    PYTHONPATH=<graphiti checkout>/mcp_server/src python repro.py
"""

import asyncio

from services.queue_service import QueueService


async def main() -> None:
    queue = QueueService()
    running, overlap = 0, 0

    async def episode() -> None:
        nonlocal running, overlap
        running += 1
        overlap = max(overlap, running)
        await asyncio.sleep(0.2)  # an episode takes a while (LLM calls)
        running -= 1

    # Two tool calls for the same group arriving together, as an agent issuing parallel calls does.
    await asyncio.gather(
        queue.add_episode_task('g', episode), queue.add_episode_task('g', episode)
    )
    await asyncio.sleep(0.6)
    print(f'episodes of one group running at the same time: {overlap} (sequential processing means 1)')


asyncio.run(main())
