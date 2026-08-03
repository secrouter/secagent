"""HTTP API service exposing user and job endpoints."""

import os

import httpx
from fastapi import FastAPI

from common.models import User

from .db import get_user, save_user

API_KEY = os.getenv("API_KEY")
WORKER_URL = "http://worker:9000/enqueue"

app = FastAPI()


@app.get("/users/{uid}")
def read_user(uid: int):
    return get_user(uid)


@app.post("/users")
def create_user(user: User):
    save_user(user)
    return {"ok": True}


@app.post("/jobs")
def enqueue_job(uid: int):
    httpx.post(WORKER_URL, json={"uid": uid})
    return {"queued": True}
