class PermissionService:
    """最小佔位的權限服務。

    後續可接入互動式授權 UI 或策略引擎。
    目前為保持同步流程可運作，預設拋出 NotImplementedError，
    讓呼叫端走保守的停止策略。
    """

    async def ask(self, *, session_id: str, permission: str, patterns: list[str] | None = None) -> None:
        raise NotImplementedError("PermissionService.ask is not implemented")
