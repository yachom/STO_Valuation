import os
import time
import hmac
import hashlib
import base64
import requests
import pandas as pd
from dotenv import load_dotenv

#실행 전 pip install python-dotenv requests pandas가 필요

load_dotenv()

BASE_URL = "https://api.searchad.naver.com"

CUSTOMER_ID = os.getenv("CUSTOMER_ID")
ACCESS_LICENSE = os.getenv("ACCESS_LICENSE")
SECRET_KEY = os.getenv("SECRET_KEY")


def make_signature(timestamp, method, uri, secret_key):
    message = f"{timestamp}.{method}.{uri}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    return base64.b64encode(signature).decode("utf-8")


def get_header(method, uri):
    timestamp = str(int(time.time() * 1000))
    signature = make_signature(timestamp, method, uri, SECRET_KEY)

    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": ACCESS_LICENSE,
        "X-Customer": CUSTOMER_ID,
        "X-Signature": signature,
    }


def get_keyword_tool(keyword):
    uri = "/keywordstool"
    method = "GET"

    params = {
        "hintKeywords": keyword,
        "showDetail": "1"
    }

    headers = get_header(method, uri)

    response = requests.get(
        BASE_URL + uri,
        headers=headers,
        params=params
    )

    if response.status_code != 200:
        print(response.status_code)
        print(response.text)
        return None

    return response.json()


result = get_keyword_tool("스타벅스더북한산점")

items = result.get("keywordList", [])
df = pd.DataFrame(items)

print(df.head())
