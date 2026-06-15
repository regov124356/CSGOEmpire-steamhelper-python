"""Shared config loading and user selection for the bot entry points."""

import json


def load_config(path: str = "config.json") -> dict:
    with open(path, "r") as file:
        return json.load(file)


def select_user(users: list) -> dict:
    """Prompt for which configured user to run as; returns that user's config."""
    print("Select a user:")
    for i, user in enumerate(users, start=1):
        print(f"{i}: {user.get('username') or f'User {i}'}")

    n = len(users)
    while True:
        try:
            choice = int(input(f"Enter user number (1-{n}): "))
            if 1 <= choice <= n:
                return users[choice - 1]
            print(f"Please select 1-{n}.")
        except ValueError:
            print("Please enter a valid number.")
