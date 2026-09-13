import os
import requests

client_id = os.environ["YOUTUBE_CLIENT_ID"]
client_secret = os.environ["YOUTUBE_CLIENT_SECRET"]
refresh_token = os.environ["YOUTUBE_REFRESH_TOKEN"]

print("client_id length:", len(client_id), "| starts:", client_id[:15], "| ends:", client_id[-25:])
print("client_secret length:", len(client_secret), "| starts:", client_secret[:8])
print("refresh_token length:", len(refresh_token), "| starts:", refresh_token[:10])

r = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    },
)
print("STATUS:", r.status_code)
print("RESPONSE:", r.text)
