import os
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("ENSEMBLE_TOKEN")

url = "https://ensembledata.com/apis/tt/user/info"

params = {
    "username": "tiktok",
    "token": TOKEN
}

res = requests.get(url, params=params)

print(res.json())