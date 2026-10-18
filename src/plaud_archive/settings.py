from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class Workflow:
    root: str
    timezone: str = "Asia/Taipei"
    start_time: str = "07:30"
    max_attempts: int = 3
    retry_seconds: int = 1800
    raw_retention_days: int = 30
    max_input_tokens: int = 256000
    audio_chunk_seconds: int = 1200
    plaud_command: str = "npx"
    plaud_args: tuple = ("-y", "@plaud-ai/mcp@0.3.13")
    groq_url: str = "https://api.groq.com/openai/v1/audio/transcriptions"
    groq_model: str = "whisper-large-v3"
    groq_key_file: str = ""
    cpa_url: str = "http://127.0.0.1:8317"
    cpa_key_file: str = ""
    gemini_model: str = "gemini-3.8-flash-high"
    fusion_model: str = "grok-4.7"
    fusion_output_tokens: int = 131072
    sensevoice_model: str = ""
    sensevoice_tokens: str = ""
    token_file: str = ""
    outbound_proxy: str = ""

    def __post_init__(self):
        if self.max_attempts < 1 or self.retry_seconds < 0 or self.raw_retention_days < 0 or self.max_input_tokens < 1 or self.audio_chunk_seconds < 1:
            raise ValueError("invalid workflow limits")
        from datetime import time
        time.fromisoformat(self.start_time)

    @classmethod
    def load(cls, filename):
        values = yaml.safe_load(Path(filename).read_text())
        return cls(**values)


def secret(name):
    value, path = os.getenv(name, ""), os.getenv(name + "_FILE", "")
    if value and path: raise ValueError(f"configure {name} or {name}_FILE, not both")
    return Path(path).read_text().strip() if path else value
