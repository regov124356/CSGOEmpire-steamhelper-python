import asyncio
from concurrent.futures import ThreadPoolExecutor
import random

from dotenv import load_dotenv
import os

from csfloatclient import CSFloatClient
from db import DB
from steamempire import SteamEmpire
from telegram import Telegram


async def checking_new_offers(steam_client: SteamEmpire):
    while True:
        print("Checking for new trade offers...")

        offers_empire = steam_client.get_active_trades_on_empire()
        if offers_empire:
            offers_steam = steam_client.get_trade_offers(use_webtoken=True)
            await steam_client.accept_matching_offers(offers_empire, offers_steam)

        await asyncio.sleep(60 + random.randint(0, 120))

async def checking_items_prices(db: DB, client: CSFloatClient):
    while True:
        print("Checking new items prices...")
        try:
            rows = db.check_item_prices()
            if not rows:
                print("No rows found in the table.")
                return

            tasks = []
            for i, entry in enumerate(rows):
                if i % 20 == 0:
                    await asyncio.sleep(61)
                id = entry[0]
                market_hash_name = entry[1]
                try:
                    empire_price, float_price = await client.check_price(market_hash_name=market_hash_name)
                    tasks.append(asyncio.create_task(db.update_items_prices(id=id, price_empire=empire_price, price_float=float_price)))
                except Exception as e:
                    print(f"Error fetching price for {market_hash_name}: {e}")

            await asyncio.gather(*tasks)
            print(f"{len(rows)} items have been checked.")

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            await asyncio.sleep(3600)


async def main():
    load_dotenv(".env")

    api_key_float = os.getenv("FLOAT_API_KEY")
    api_key_steam = os.getenv("STEAM_API_KEY")
    username = os.getenv("STEAM_USERNAME")
    password = os.getenv("STEAM_PASSWORD")
    steam_guard_path = os.getenv("STEAM_STEAM_GUARD_PATH")

    bearer_auth = os.getenv("EMPIRE_BEARER_AUTH")

    t_token = os.getenv("TELEGRAM_TOKEN")
    t_chat_id = os.getenv("TELEGRAM_CHAT_ID")
    telegram = Telegram(t_token, t_chat_id)

    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_database = os.getenv("DB_DATABASE")
    db_host = os.getenv("DB_HOST")

    divider = float(os.getenv("DIVIDER"))

    db = DB(user=db_user, password=db_password, host=db_host, database=db_database)

    steam_client = SteamEmpire(api_key=api_key_steam, username=username, password=password,
                               steam_guard=steam_guard_path, bearer_auth=bearer_auth, telegram=telegram, db=db, divider=divider)

    client = CSFloatClient(api_key=api_key_float)
    client.set_divider(divider=divider)

    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)

    task1_thread = loop.run_in_executor(executor, lambda: asyncio.run(checking_new_offers(steam_client)))
    task2_thread = loop.run_in_executor(executor, lambda: asyncio.run(checking_items_prices(db, client)))

    await asyncio.gather(task1_thread, task2_thread)

if __name__ == "__main__":
    asyncio.run(main())