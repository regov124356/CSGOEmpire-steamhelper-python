"""Entry point for the auction bidder (separate process from main.py)."""

import asyncio

from config_loader import load_config, select_user
from db import DB
from csgoempire_client import CSGOEmpireClient
from bidding_bot import BiddingBot


async def main():
    data = load_config()
    user = select_user(data["users"])
    bearer_auth = user["empire"]["api_key"]

    db_cfg = data["db"]
    db = DB(user=db_cfg["user"], password=db_cfg["password"],
            host=db_cfg["host"], database=db_cfg["database"])

    async with CSGOEmpireClient(bearer_auth) as empire:
        bot = BiddingBot(empire=empire, db=db)
        await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
