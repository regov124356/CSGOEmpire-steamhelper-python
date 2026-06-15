import asyncio

from config_loader import load_config, select_user
from csfloatclient import CSFloatClient
from csgoempire_client import CSGOEmpireClient
from db import DB

from price_service import PriceService
from steam_client import SteamClient
from telegram import Telegram
from trade_bot import TradeBot


async def main():
    data = load_config()
    user = select_user(data["users"])

    api_key_float = user["float"]["api_key"]
    api_key_steam = user["steam"]["api_key"]
    username = user["steam"]["username"]
    password = user["steam"]["password"]
    steam_guard_path = user["steam"].get("steam_guard_path")

    bearer_auth = user["empire"]["api_key"]

    t_token = data["telegram"]["token"]
    t_chat_id = data["telegram"]["chat_id"]
    telegram = Telegram(t_token, t_chat_id)

    db_user = data["db"]["user"]
    db_password = data["db"]["password"]
    db_database = data["db"]["database"]
    db_host = data["db"]["host"]

    divider = float(data["divider"])

    db = DB(user=db_user, password=db_password, host=db_host, database=db_database)

    steam_client = SteamClient(api_key=api_key_steam, username=username, password=password,
                               steam_guard=steam_guard_path)

    async with CSGOEmpireClient(bearer_auth) as empire, \
            CSFloatClient(api_key_float) as csfloat:
        bot = TradeBot(steam=steam_client, empire=empire, db=db,
                       telegram=telegram, divider=divider)
        price_service = PriceService(client=csfloat, db=db, divider=divider)
        await asyncio.gather(
            bot.run(),
            price_service.run()
        )


if __name__ == "__main__":
    asyncio.run(main())