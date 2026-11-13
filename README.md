# Phone-call agent

Text a phone number and a goal to your bot. An AI agent calls that person, has the conversation,
and texts you a summary of what they said.

```text
You:  Call +1 555 123 4567 and ask if they're free for dinner Friday at 7.
Bot:  Calling +15551234567 now. Goal: Ask if they're free for dinner Friday at 7.
...
Bot:  Call with +15551234567:
      Yes, free Friday, but 7:30 works better. Asked if they should bring anything.
```

## How it works

1. You text your Twilio number. Claude reads the message and pulls out the number and the goal.
   Only texts from `OWNER_PHONE_NUMBER` are accepted.
2. The server places an outbound call through Twilio. Twilio ConversationRelay handles
   speech-to-text and text-to-speech, and streams the conversation to the server over a WebSocket.
3. Claude (`claude-opus-5-5` at low effort, to keep replies fast) runs the conversation and hangs up
   when the goal is done.
4. When the call ends, Claude summarizes the transcript and the server texts you the summary. If
   nobody answers or the line is busy, you get a text saying so.

## Setup

You need a Twilio account with a voice- and SMS-capable phone number, and an Anthropic API key.

1. In the Twilio Console, enable ConversationRelay for your account. It's under the Voice AI
   features and requires accepting the AI/ML addendum.
2. Install dependencies:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in every value.
4. Expose the server on a public HTTPS address. For local testing, use a tunnel such as
   ngrok (`ngrok http 8000`), and put the HTTPS address it gives you in `PUBLIC_URL`.
5. Start the server:

   ```bash
   set -a; source .env; set +a
   .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
   ```

6. In the Twilio Console, open your phone number and set **A message comes in** to a webhook:
   `POST <PUBLIC_URL>/sms`.
7. Text your Twilio number from your own phone.

Run one server process only: call state is kept in memory.

## Calling rules

AI-voice calls are regulated. In the US, the FCC treats AI-generated voices as "artificial" voices
under the Telephone Consumer Protection Act (TCPA), and many states require everyone's consent to
record or transcribe a call. The agent introduces itself as an AI assistant at the start of every
call. Use it to call people and businesses who'd expect to hear from you, and don't use it for
marketing or bulk calling.
