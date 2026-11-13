"""Phone-call agent: text it who to call and why, it calls them, then texts you what they said.

Flow:
1. You text your Twilio number, for example "Call +1 555 123 4567 and ask if they're free Friday."
2. `/sms` reads the request with Claude and places an outbound call through Twilio.
3. When the person answers, Twilio ConversationRelay handles speech-to-text and text-to-speech
   and streams the conversation over the `/relay/{call_id}` WebSocket, where Claude responds.
4. When the call ends, Claude summarizes the transcript and the summary is texted to you.
"""

import asyncio
import contextlib
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.messaging_response import MessagingResponse
from twilio.twiml.voice_response import Connect, VoiceResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("caller")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name}. See .env.example.")
    return value


TWILIO_ACCOUNT_SID = require_env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = require_env("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = require_env("TWILIO_PHONE_NUMBER")
OWNER_PHONE_NUMBER = require_env("OWNER_PHONE_NUMBER")
PUBLIC_URL = require_env("PUBLIC_URL").rstrip("/")
OWNER_NAME = os.environ.get("OWNER_NAME", "").strip() or "my user"
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+1").strip()
VOICE = os.environ.get("TTS_VOICE", "").strip()  # Optional ConversationRelay voice override.

MODEL = "claude-opus-5-5"
# Phone conversations are latency-sensitive, so the live call runs at low effort.
CALL_EFFORT = "low"
FALLBACK_BETAS = ["server-side-fallback-2026-07-01"]
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
FINAL_CALL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}

claude = anthropic.AsyncAnthropic()
twilio = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
validator = RequestValidator(TWILIO_AUTH_TOKEN)
app = FastAPI()

# Keep references to background tasks so they aren't garbage-collected mid-run.
_background: set[asyncio.Task] = set()


def run_in_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------------------
# Call state (in memory: run a single server process)
# ---------------------------------------------------------------------------


@dataclass
class CallSession:
    id: str
    to: str
    goal: str
    greeting: str
    call_sid: str | None = None
    # Claude conversation history. Append-only: assistant turns are stored exactly as returned.
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Human-readable transcript for the summary: (speaker, text).
    transcript: list[tuple[str, str]] = field(default_factory=list)
    pending_note: str = ""  # Context to attach to the caller's next turn, such as an interruption.
    relay_started: bool = False
    relay_done: asyncio.Event = field(default_factory=asyncio.Event)
    ending: bool = False
    reported: bool = False

    def add_caller_turn(self, text: str) -> None:
        self.transcript.append(("Them", text))
        if self.pending_note:
            text = f"{self.pending_note}\n\n{text}"
            self.pending_note = ""
        if self.messages and self.messages[-1]["role"] == "user":
            # The caller spoke again before we replied: fold it into the same turn.
            self.messages[-1]["content"] += f"\n{text}"
        else:
            self.messages.append({"role": "user", "content": text})

    @property
    def heard_from_them(self) -> bool:
        return any(speaker == "Them" for speaker, _ in self.transcript)


calls: dict[str, CallSession] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def verify_twilio_request(request: Request, params: dict[str, str]) -> None:
    """Reject webhook requests that weren't signed by Twilio."""
    url = f"{PUBLIC_URL}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


async def text_owner(body: str) -> None:
    body = body.strip()
    if len(body) > 1500:
        body = body[:1497] + "..."
    await asyncio.to_thread(
        twilio.messages.create, to=OWNER_PHONE_NUMBER, from_=TWILIO_PHONE_NUMBER, body=body
    )


def twiml_reply(text: str) -> Response:
    reply = MessagingResponse()
    reply.message(text)
    return Response(content=str(reply), media_type="application/xml")


def final_text(message: Any) -> str:
    return "".join(block.text for block in message.content if block.type == "text").strip()


# ---------------------------------------------------------------------------
# Step 1: read the owner's text message
# ---------------------------------------------------------------------------


class CallRequest(BaseModel):
    is_call_request: bool
    phone_number: str
    goal: str


PARSE_SYSTEM = f"""You read text messages that ask a phone agent to call someone.
Extract:
- is_call_request: true only if the message asks to call a specific phone number.
- phone_number: the number in E.164 format (for example +15551234567). If the message has no
  country code, assume {DEFAULT_COUNTRY_CODE}. Use an empty string if there's no number.
- goal: what the agent should accomplish on the call, written as instructions to the agent,
  including any details from the message the agent will need. Empty string if none."""


async def parse_request(text: str) -> CallRequest | None:
    response = await claude.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        betas=FALLBACK_BETAS,
        fallbacks="default",
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": text}],
        output_format=CallRequest,
    )
    if response.stop_reason == "refusal":
        return None
    return response.parsed_output


@app.post("/sms")
async def incoming_sms(request: Request) -> Response:
    params = dict(await request.form())
    verify_twilio_request(request, params)

    if params.get("From") != OWNER_PHONE_NUMBER:
        log.warning("Ignored a text from an unknown number")
        return Response(content=str(MessagingResponse()), media_type="application/xml")

    body = params.get("Body", "").strip()
    parsed = await parse_request(body)
    if parsed is None:
        return twiml_reply("Sorry, I couldn't process that request.")
    if not parsed.is_call_request or not parsed.goal:
        return twiml_reply(
            'Text me who to call and what to ask, like: "Call +1 555 123 4567 and ask '
            "if they're free for dinner Friday.\""
        )
    if not E164.match(parsed.phone_number):
        return twiml_reply(f"That doesn't look like a valid phone number: {parsed.phone_number}")

    session = start_session(parsed.phone_number, parsed.goal)
    try:
        await place_call(session)
    except Exception:
        log.exception("Couldn't place the call")
        calls.pop(session.id, None)
        return twiml_reply(f"I couldn't place the call to {session.to}. Check the server logs.")

    return twiml_reply(f"Calling {session.to} now. Goal: {session.goal}")


# ---------------------------------------------------------------------------
# Step 2: place the call
# ---------------------------------------------------------------------------


def start_session(to: str, goal: str) -> CallSession:
    call_id = secrets.token_urlsafe(24)  # Unguessable: it also authorizes the relay WebSocket.
    greeting = (
        f"Hi, this is an AI assistant calling on behalf of {OWNER_NAME}. "
        "Do you have a quick minute?"
    )
    session = CallSession(id=call_id, to=to, goal=goal, greeting=greeting)
    session.transcript.append(("Agent", greeting))
    calls[call_id] = session
    return session


async def place_call(session: CallSession) -> None:
    ws_base = PUBLIC_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    response = VoiceResponse()
    connect = Connect()
    relay_options = {"url": f"{ws_base}/relay/{session.id}", "welcome_greeting": session.greeting}
    if VOICE:
        relay_options["voice"] = VOICE
    connect.conversation_relay(**relay_options)
    response.append(connect)

    call = await asyncio.to_thread(
        twilio.calls.create,
        to=session.to,
        from_=TWILIO_PHONE_NUMBER,
        twiml=str(response),
        status_callback=f"{PUBLIC_URL}/call-status/{session.id}",
        status_callback_event=["completed"],
    )
    session.call_sid = call.sid
    log.info("Placed call %s", call.sid)


# ---------------------------------------------------------------------------
# Step 3: the live conversation
# ---------------------------------------------------------------------------

END_CALL_TOOL = {
    "name": "end_call",
    "description": (
        "Hang up the phone. Call this after you've said goodbye, once the goal is done, the "
        "person asks to end the call, or you've reached voicemail and left a message."
    ),
    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    "strict": True,
    "eager_input_streaming": True,
}


def call_system_prompt(session: CallSession) -> str:
    return f"""You are an AI assistant on a live phone call, calling on behalf of {OWNER_NAME}.

Your goal for this call:
{session.goal}

The call opened with you saying: "{session.greeting}"

How to talk:
- Everything you write is spoken aloud by text-to-speech. Write plain conversational sentences:
  no markdown, lists, emoji, or symbols. Write numbers, times, and dates the way you'd say them.
- Keep each reply to one or two short sentences, then let them talk.
- Be polite, warm, and direct. Ask one question at a time.
- Be honest that you're an AI assistant if it comes up. Never pretend to be {OWNER_NAME}.
- Don't agree to anything, share personal information, or make commitments beyond what the goal
  allows. If they ask something you can't answer, say you'll pass the question along.
- If you reach voicemail or an automated menu you can't get past, leave a brief message that
  covers the goal, then end the call.
- If they're busy or don't want to talk, thank them and end the call.

When the goal is done, confirm what you heard, say a short goodbye, and then call the end_call
tool in the same reply. Messages in [brackets] describe what happened on the call; they aren't
words the person said."""


async def respond(ws: WebSocket, session: CallSession) -> None:
    """Stream Claude's reply to ConversationRelay, which speaks it as text arrives."""
    spoken: list[str] = []
    try:
        async with claude.beta.messages.stream(
            model=MODEL,
            max_tokens=16000,
            betas=FALLBACK_BETAS,
            fallbacks="default",
            output_config={"effort": CALL_EFFORT},
            system=call_system_prompt(session),
            tools=[END_CALL_TOOL],
            messages=session.messages,
        ) as stream:
            async for text in stream.text_stream:
                spoken.append(text)
                await ws.send_json({"type": "text", "token": text, "last": False})
            message = await stream.get_final_message()
    except asyncio.CancelledError:
        # The caller interrupted while the reply was still generating. Record what went out.
        partial = "".join(spoken).strip()
        if partial:
            session.messages.append({"role": "assistant", "content": partial})
            session.transcript.append(("Agent", partial + " [interrupted]"))
        raise

    # Store the reply exactly as returned (thinking blocks included) so history stays append-only.
    session.messages.append({"role": "assistant", "content": message.content})
    await ws.send_json({"type": "text", "token": "", "last": True})

    if message.stop_reason == "refusal":
        log.warning("The model declined to continue the call")
        await hang_up(ws, session, "")
        return

    said = final_text(message)
    if said:
        session.transcript.append(("Agent", said))

    if any(block.type == "tool_use" and block.name == "end_call" for block in message.content):
        await hang_up(ws, session, said)


async def hang_up(ws: WebSocket, session: CallSession, last_words: str) -> None:
    session.ending = True
    # Give text-to-speech time to finish the goodbye (about 2.5 words a second, plus a pause).
    await asyncio.sleep(len(last_words.split()) / 2.5 + 1.5)
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "end"})


@app.websocket("/relay/{call_id}")
async def relay(ws: WebSocket, call_id: str) -> None:
    session = calls.get(call_id)
    if session is None or session.relay_started:
        await ws.close(code=1008)
        return
    await ws.accept()
    session.relay_started = True
    current: asyncio.Task | None = None

    async def cancel_current() -> None:
        if current and not current.done():
            current.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current

    try:
        while True:
            event = await ws.receive_json()
            kind = event.get("type")

            if kind == "setup":
                log.info("Relay connected for call %s", event.get("callSid"))

            elif kind == "prompt":
                text = (event.get("voicePrompt") or "").strip()
                if not text or session.ending:
                    continue
                await cancel_current()
                session.add_caller_turn(text)
                current = asyncio.create_task(respond(ws, session))

            elif kind == "dtmf":
                if session.ending:
                    continue
                await cancel_current()
                session.add_caller_turn(f"[They pressed the {event.get('digit')} key]")
                current = asyncio.create_task(respond(ws, session))

            elif kind == "interrupt":
                await cancel_current()
                heard = (event.get("utteranceUntilInterrupt") or "").strip()
                session.pending_note = (
                    f'[They interrupted you. They only heard: "{heard}"]'
                    if heard
                    else "[They interrupted you before hearing your last reply]"
                )

            elif kind == "error":
                log.error("ConversationRelay error: %s", event.get("description"))

    except WebSocketDisconnect:
        pass
    finally:
        await cancel_current()
        session.relay_done.set()


# ---------------------------------------------------------------------------
# Step 4: report back when the call ends
# ---------------------------------------------------------------------------

STATUS_TEXT = {
    "busy": "the line was busy",
    "no-answer": "nobody answered",
    "failed": "the call failed",
    "canceled": "the call was canceled",
    "completed": "the call connected, but nobody said anything",
}


@app.post("/call-status/{call_id}")
async def call_status(request: Request, call_id: str) -> Response:
    params = dict(await request.form())
    verify_twilio_request(request, params)
    status = params.get("CallStatus", "")
    session = calls.get(call_id)
    if session and status in FINAL_CALL_STATUSES:
        run_in_background(report(session, status))
    return Response(status_code=204)


async def report(session: CallSession, status: str) -> None:
    if session.relay_started:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(session.relay_done.wait(), timeout=30)
    if session.reported:
        return
    session.reported = True

    try:
        if not session.heard_from_them:
            await text_owner(f"Called {session.to}: {STATUS_TEXT.get(status, status)}.")
        else:
            summary = await summarize(session)
            await text_owner(f"Call with {session.to}:\n{summary}")
    except Exception:
        log.exception("Couldn't send the call report")
    finally:
        calls.pop(session.id, None)


async def summarize(session: CallSession) -> str:
    transcript = "\n".join(f"{speaker}: {text}" for speaker, text in session.transcript)
    response = await claude.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=FALLBACK_BETAS,
        fallbacks="default",
        system=(
            "You summarize phone calls an AI assistant made on someone's behalf. The summary is "
            "sent as a text message, so keep it under 600 characters, in plain text with no "
            "markdown. Lead with the direct answer to the goal (or say it wasn't reached), then "
            "any other important things the person said, quoting them where exact wording "
            "matters. Don't describe what the assistant said unless it matters."
        ),
        messages=[
            {
                "role": "user",
                "content": f"Goal of the call:\n{session.goal}\n\nTranscript:\n{transcript}",
            }
        ],
    )
    if response.stop_reason == "refusal":
        return f"(Summary unavailable.) Transcript:\n{transcript}"
    return final_text(response) or transcript


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
