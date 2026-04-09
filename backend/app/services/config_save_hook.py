from app.services.log_hub import LogLevel


async def push_log_chunks(hub, level: LogLevel, text: str, max_total: int = 24000, chunk: int = 2000) -> None:
    """长文本分块写入日志（保留供其它模块按需使用）。"""
    if len(text) > max_total:
        text = text[:max_total] + "\n… (已截断)"
    for i in range(0, len(text), chunk):
        await hub.push(level, text[i : i + chunk])
