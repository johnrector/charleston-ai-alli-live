import os
from tac import TAC, TACConfig
from tac.channels.voice import VoiceChannel
from tac.channels.voice.media_streams.gpt_live import (
    TWILIO_AUDIO_FORMAT_FOR_GPT_LIVE,
    GPTLiveProviderConfig,
)
from tac.server import TACFastAPIServer
from tac.models.outbound import InitiateVoiceConversationOptions
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from tac.tools import function_tool

FOREGROUND = """You are Alli, John Rector's personal AI. You are not a receptionist, sales bot, call-center agent, or generic assistant.
John Rector is a Charleston-area entrepreneur and former IBM executive. He co-founded E2open and now runs Charleston AI.
John owns three Charleston-area homes: 4003 Waterway Boulevard on Isle of Palms; 2630 Middle Street on Sullivan's Island; and 1209 Myrick Road in Mount Pleasant.
All Charleston-area scheduling defaults to America/New_York. Never ask the timezone for a local meeting.
Treat John's identity, the three properties, the recipient identity, and the current mission as immediate foreground knowledge. Never delegate or say you need to check these facts.
Keep turns short, fast, natural, and interruptible. Avoid sales-call filler.
Never invent facts, appointments, promises, or tool results.
Delegate only genuine actions such as checking live calendar availability, creating an event, or sending email.
"""

@function_tool()
def demo_capability(action: str) -> str:
    """Temporary diagnostic tool proving GPT-Live -> GPT-5.6 Sol Responses delegation -> TAC tool execution."""
    return f"Delegation is working. Requested action: {action}"

tac = TAC(config=TACConfig.from_env())

SESSION_CONFIG = {
    "model": "gpt-live-1",
    "instructions": FOREGROUND,
    "audio": {
        "format": TWILIO_AUDIO_FORMAT_FOR_GPT_LIVE,
        "output": {"voice": "marin"},
    },
    "delegation": {
        "type": "responses",
        "responses": {
            "model": "gpt-5.6-sol",
            "tools": [demo_capability.to_realtime_format()],
            "tool_choice": "auto",
        },
    },
}

app = FastAPI()

class DemoCall(BaseModel):
    to: str
    name: str = "there"
    brief: str = "John asked me to call and have a short conversation."

@app.get("/health")
async def health():
    return {"ok": True, "voice_model": "gpt-live-1", "reasoning_model": "gpt-5.6-sol"}

@app.post("/demo-call")
async def demo_call(body: DemoCall, x_demo_key: str = Header(default="")):
    expected = os.environ.get("DEMO_KEY", "")
    if not expected or x_demo_key != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    domain = os.environ["TWILIO_VOICE_PUBLIC_DOMAIN"]
    result = await voice_channel.initiate_outbound_conversation(
        InitiateVoiceConversationOptions(
            to=body.to,
            websocket_url=f"wss://{domain}/ws",
        )
    )
    return {"ok": True, "call_sid": result.call_sid, "brief": body.brief}

voice_channel = VoiceChannel(
    tac,
    config=GPTLiveProviderConfig(
        tools=[demo_capability],
        default_session_config=SESSION_CONFIG,
        welcome_instruction=(
            "Greet the person immediately and naturally: Hi, I'm Alli, John Rector's AI. "
            "John asked me to call you. Then pause and listen."
        ),
    ),
)

if __name__ == "__main__":
    server = TACFastAPIServer(tac=tac, voice_channel=voice_channel, app=app)
    server.start()
