"""Shared domain models used across services."""

from dataclasses import dataclass


@dataclass
class User:
    id: int
    name: str
    email: str


@dataclass
class Job:
    id: int
    user_id: int
    payload: str
