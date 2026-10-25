"""SMS wire limits, independent of conversation and executive reasoning.

GSM extension characters consume two septets; other text uses UTF-16 units.
Keep a conservative eight-segment application budget for the local sender.
Literal messages are validated, never truncated or rewritten by transport.
"""
from math import ceil

from gsm0338 import Codec

from .errors import InvalidInput


MAX_SEGMENTS = 8
_codec = Codec()


def segments(body: str) -> int:
    if not isinstance(body, str) or not body.strip():
        raise InvalidInput('Supply a nonempty SMS body')
    try:
        units = len(_codec.encode(body)[0])
        single, combined = 160, 153
    except UnicodeEncodeError:
        try:
            units = len(body.encode('utf-16-be')) // 2
        except UnicodeEncodeError:
            raise InvalidInput('SMS contains an invalid Unicode sequence') from None
        single, combined = 70, 67
    return 1 if units <= single else ceil(units / combined)


def validate(body: str) -> None:
    if segments(body) > MAX_SEGMENTS:
        raise InvalidInput('SMS exceeds the eight-segment delivery budget. '
                           'Write a shorter message or deliver the full content by email.')


def fits(body: str) -> bool:
    try:
        validate(body)
        return True
    except InvalidInput:
        return False


def result_message(summary: str, notification: str | None, *, owner_url: str | None) -> str:
    """Keep short results intact; use the model's notice for longer work.

    Older native sessions can finish without a notification field. Their full
    result is still retained; the fallback makes no claim that actions succeeded.
    Only the application supplies an authenticated owner's private result URL.
    """
    summary = summary.strip()
    if fits(summary):
        return summary
    fallback = ('Your full update is saved in your private dashboard.' if owner_url
                else 'I have a longer update saved. Call me and I can go through it with you.')
    brief = (notification or '').strip() or fallback
    suffix = '\nFull update: ' + owner_url if owner_url else ''
    body = brief + suffix
    if not fits(body):
        body = fallback + suffix
    validate(body)
    return body
