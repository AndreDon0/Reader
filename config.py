from dotenv import load_dotenv
import os
from sqlalchemy.engine import URL

load_dotenv()
POSTGRES_PASSWORD = os.environ["POSTGRES_PASSWORD"]
VPN_SUBSCRIPTION_URL = os.environ["VPN_SUBSCRIPTION_URL"]

DATABASE_URL = URL.create(
    drivername="postgresql+psycopg",
    username="reader",
    password=os.environ["POSTGRES_PASSWORD"],
    host="127.0.0.1",
    port=60209,
    database="reader",
)

FILES_PATH = "/srv/storage/reader/files"