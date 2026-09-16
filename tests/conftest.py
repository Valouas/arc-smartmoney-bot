"""Tests never load the user's .env (including a parent workspace .env)."""
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False
