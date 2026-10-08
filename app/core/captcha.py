from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.core.exceptions import BadRequestError

logger = logging.getLogger(__name__)

RECAPTCHA_VERIFY_URL = "https://www.google.com/recaptcha/api/siteverify"


async def verify_captcha(token: str) -> float:
    """Verify a reCAPTCHA v3 token with Google's API.

    Args:
        token: The reCAPTCHA response token from the client.

    Returns:
        The score (0.0 to 1.0).

    Raises:
        BadRequestError: If verification fails or score is below threshold.
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            RECAPTCHA_VERIFY_URL,
            data={
                "secret": settings.RECAPTCHA_SECRET_KEY,
                "response": token,
            },
        )

    result = response.json()

    if not result.get("success", False):
        logger.warning("reCAPTCHA verification failed: %s", result.get("error-codes"))
        raise BadRequestError(
            code="CAPTCHA_FAILED",
            message="CAPTCHA verification failed. Please try again.",
        )

    score = result.get("score", 0.0)
    if score < settings.RECAPTCHA_SCORE_THRESHOLD:
        logger.warning("reCAPTCHA score too low: %.2f (threshold: %.2f)", score, settings.RECAPTCHA_SCORE_THRESHOLD)
        raise BadRequestError(
            code="CAPTCHA_FAILED",
            message="Automated submission detected. Please try again.",
        )

    return score
