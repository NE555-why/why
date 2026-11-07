"""LLM 客户端的可分类异常。"""


class LLMError(RuntimeError):
    """模型调用失败。"""


class FatalLLMError(LLMError):
    """无需重试的致命模型错误。"""

    fatal = True


class EmptyResponseError(LLMError):
    """服务端没有返回可用 choices。"""


class TruncatedError(LLMError):
    """模型输出因 token 上限被截断。"""
