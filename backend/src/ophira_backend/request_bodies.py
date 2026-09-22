"""Bounded request parsing shared by the app and the offline maintenance UI."""

from fastapi import HTTPException, Request


async def read_bounded_body(request: Request, *, max_bytes: int, too_large_detail: str) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length:
        try:
            declared_bytes = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        if declared_bytes < 0:
            raise HTTPException(status_code=400, detail="invalid Content-Length")
        if declared_bytes > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        body.extend(chunk)
    return bytes(body)
