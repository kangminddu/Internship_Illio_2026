import asyncio
from TikTokApi import TikTokApi


async def main():
    async with TikTokApi() as api:

        await api.create_sessions(
            num_sessions=1,
            headless=False,
            browser="webkit"
        )

        user = api.user(username="tiktok")

        async for video in user.videos(count=5):

            print(video.as_dict)

            break


asyncio.run(main())