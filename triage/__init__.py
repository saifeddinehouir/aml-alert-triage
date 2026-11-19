from pathlib import Path

from dotenv import load_dotenv

# Real environment variables win over .env.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
