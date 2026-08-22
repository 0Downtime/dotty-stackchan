import asyncio
import logging
import time

import websockets
from config.logger import setup_logging


class SuppressInvalidHandshakeFilter(logging.Filter):
    """过滤掉无效握手错误日志（如HTTPS访问WS端口）"""

    def filter(self, record):
        msg = record.getMessage()
        suppress_keywords = [
            "opening handshake failed",
            "did not receive a valid HTTP request",
            "connection closed while reading HTTP request",
            "line without CRLF",
        ]
        return not any(keyword in msg for keyword in suppress_keywords)


def _setup_websockets_logger():
    """配置 websockets 相关的所有 logger，过滤无效握手错误"""
    filter_instance = SuppressInvalidHandshakeFilter()
    for logger_name in ["websockets", "websockets.server", "websockets.client"]:
        logger = logging.getLogger(logger_name)
        logger.addFilter(filter_instance)


_setup_websockets_logger()


from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api_async
from core.auth import AuthManager, AuthenticationError
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update
# DOTTY-PATCH: shared registry consumed by the admin /inject-text route.
from core.portal_bridge import active_connections as _dotty_active_connections

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.config_lock = asyncio.Lock()
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        async with websockets.serve(
            self._handle_connection, host, port, process_request=self._http_response
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # 尝试从 URL 的查询参数中获取 device-id
            from urllib.parse import parse_qs, urlparse

            # 从 WebSocket 请求中获取路径
            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send("端口正常，如需测试连接，请使用test_page.html")
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 先认证，后建立连接
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("认证失败")
            await websocket.close()
            return
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # 传入server实例
        )
        # DOTTY-PATCH: optional OpenAI Realtime speech-to-speech route. The
        # wrapper is installed only when DOTTY_REALTIME_ENABLED=true; every
        # declined message continues through ConnectionHandler unchanged.
        _dotty_realtime = None
        try:
            from core.providers.realtime import attach_realtime_bridge

            _dotty_realtime = attach_realtime_bridge(handler)
        except Exception as exc:
            self.logger.bind(tag=TAG).warning(
                "OpenAI Realtime bridge unavailable at startup "
                f"({type(exc).__name__}); local voice path remains active"
            )
        # DOTTY-PATCH: register this connection so the admin HTTP route can
        # find it. Use the request header (the protocol-mandated identifier).
        _dotty_dev_id = websocket.request.headers.get("device-id", "") or ""
        _dotty_started = time.monotonic()
        _dotty_peer = getattr(websocket, "remote_address", None)
        self.logger.bind(tag=TAG).info(
            "DOTTY_CONN_OPEN "
            f"device={_dotty_dev_id or '-'} peer={_dotty_peer!r} "
            f"path={getattr(websocket.request, 'path', '-')!r}"
        )
        if _dotty_dev_id:
            _dotty_active_connections[_dotty_dev_id] = handler
        _dotty_handler_error = None
        _dotty_face_sync_task = None
        if _dotty_dev_id:
            async def _sync_face_bundle_after_connect():
                # handle_connection installs websocket/session_id. Wait for
                # that seam, then reassert server desired state immediately.
                for _ in range(100):
                    if getattr(handler, "websocket", None) is not None:
                        break
                    await asyncio.sleep(0.05)
                if getattr(handler, "websocket", None) is None:
                    return
                try:
                    from core.utils.face_bundles import FaceBundleStore
                    from core.utils.device_command import call_tool
                    desired = FaceBundleStore().get(_dotty_dev_id)
                    pack_id = desired.get("face_pack_id", "classic")
                    handler._dotty_requested_face_pack = pack_id
                    handler._dotty_face_pack_pending = True
                    await call_tool(
                        handler, "self.robot.set_face_pack", {"pack_id": pack_id},
                    )
                except Exception as sync_error:
                    self.logger.bind(tag=TAG).warning(
                        "face bundle reconnect sync failed "
                        f"({type(sync_error).__name__})"
                    )
            _dotty_face_sync_task = asyncio.create_task(
                _sync_face_bundle_after_connect(), name="face_bundle_reconnect_sync"
            )
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            _dotty_handler_error = e
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            if _dotty_face_sync_task is not None and not _dotty_face_sync_task.done():
                _dotty_face_sync_task.cancel()
            if _dotty_realtime is not None:
                try:
                    await _dotty_realtime.close()
                except Exception as realtime_close_error:
                    self.logger.bind(tag=TAG).warning(
                        "OpenAI Realtime bridge close failed: "
                        f"{type(realtime_close_error).__name__}"
                    )
            # DOTTY-PATCH: pop only if the entry still points at this handler
            # (a quick reconnect with the same device-id may have replaced it).
            if _dotty_dev_id and _dotty_active_connections.get(_dotty_dev_id) is handler:
                _dotty_active_connections.pop(_dotty_dev_id, None)
            # DOTTY-PATCH: log transport-level closure evidence before forcing
            # a server-side close.  The upstream ConnectionHandler consumes
            # ConnectionClosed and only emits a generic "client disconnected"
            # message, which cannot distinguish a clean session end from a
            # Wi-Fi loss, brownout, watchdog reset, or firmware crash.  The
            # websockets close code is 1006 when no close frame was received.
            _dotty_pre_state = getattr(
                getattr(websocket, "state", None), "name", "UNKNOWN"
            )
            _dotty_pre_code = getattr(websocket, "close_code", None)
            _dotty_pre_reason = getattr(websocket, "close_reason", None)
            _dotty_duration_ms = int((time.monotonic() - _dotty_started) * 1000)
            _dotty_error_name = (
                type(_dotty_handler_error).__name__
                if _dotty_handler_error is not None
                else "none"
            )
            self.logger.bind(tag=TAG).warning(
                "DOTTY_CONN_END "
                f"device={_dotty_dev_id or '-'} peer={_dotty_peer!r} "
                f"duration_ms={_dotty_duration_ms} pre_state={_dotty_pre_state} "
                f"close_code={_dotty_pre_code!r} "
                f"close_reason={_dotty_pre_reason!r} "
                f"handler_error={_dotty_error_name}"
            )
            # 强制关闭连接（如果还没有关闭的话）
            try:
                # 安全地检查WebSocket状态并关闭
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # 如果没有closed属性，直接尝试关闭
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _http_response(self, websocket, request_headers):
        # 检查是否为 WebSocket 升级请求
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            # 如果是 WebSocket 请求，返回 None 允许握手继续
            return None
        else:
            # 如果是普通 HTTP 请求，返回 "server is running"
            return websocket.respond(200, "Server is running\n")

    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                new_config = await get_config_from_api_async(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("获取新配置失败")
                    return False
                self.logger.bind(tag=TAG).info("获取新配置成功")
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查VAD和ASR类型是否需要更新: {update_vad} {update_asr}"
                )
                self.config = new_config
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    False,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )
                if "vad" in modules:
                    self._vad = modules["vad"]
                if "asr" in modules:
                    self._asr = modules["asr"]
                if "llm" in modules:
                    self._llm = modules["llm"]
                if "intent" in modules:
                    self._intent = modules["intent"]
                if "memory" in modules:
                    self._memory = modules["memory"]
                self.logger.bind(tag=TAG).info("更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        if self.auth_enable:
            headers = dict(websocket.request.headers)
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                return
            else:
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    token = token[7:]
                else:
                    raise AuthenticationError("Missing or invalid Authorization header")
                auth_success = self.auth.verify_token(
                    token, client_id=client_id, username=device_id
                )
                if not auth_success:
                    raise AuthenticationError("Invalid token")
