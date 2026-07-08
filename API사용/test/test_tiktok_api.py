import asyncio
from TikTokApi import TikTokApi


async def main():
    api = TikTokApi()

    await api.create_sessions(
        num_sessions=1,
        headless=False
    )

    user = api.user(username="tiktok")

    info = await user.info()

    print(info)

    await api.close_sessions()


asyncio.run(main())