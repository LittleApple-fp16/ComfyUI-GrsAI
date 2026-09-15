"""
GrsAI API客户端
封装与grsai.com的所有交互逻辑
"""

import json
import requests
from typing import Optional, Dict, Any, List, Tuple, TYPE_CHECKING
from concurrent.futures import ThreadPoolExecutor, as_completed

if TYPE_CHECKING:
    from PIL import Image

try:
    from .config import GrsaiConfig, default_config
    from .utils import format_error_message, download_image
except ImportError:
    from config import GrsaiConfig, default_config
    from utils import format_error_message, download_image


try:
    from .generation_api import GenerationAPI, GrsaiAPIError
except ImportError:
    from generation_api import GenerationAPI, GrsaiAPIError


class GrsaiAPI(GenerationAPI):
    def __init__(self, api_key: str, config: Optional[GrsaiConfig] = None):
        """
        初始化API客户端

        Args:
            api_key: API密钥
            config: 配置对象
        """
        if not api_key or not api_key.strip():
            raise GrsaiAPIError((config or default_config).api_key_error_message)

        self.api_key = api_key.strip()
        self.config = config or default_config
        self.session = requests.Session()
        self._setup_session()

    def _setup_session(self):
        """设置HTTP会话"""
        self.session.headers.update(
            {
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "ComfyUI-GrsAI/1.0",
            }
        )

        # 直接使用传入的API密钥设置认证头
        self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    def flux_generate_image(
        self,
        prompt: str,
        model: str = "flux-kontext-pro",
        seed: Optional[int] = None,
        aspect_ratio: Optional[str] = None,
        urls: List[str] = [],
        output_format: Optional[str] = None,
        safety_tolerance: Optional[int] = None,
        prompt_upsampling: Optional[bool] = None,
        guidance_scale: Optional[float] = None,
        num_inference_steps: Optional[int] = None,
    ) -> Tuple["Image.Image", str]:
        # 构建请求数据
        payload = {
            "model": model,
            "prompt": prompt,
            "urls": urls,
            "shutProgress": True,
            "cdn": "zh",
        }

        # 动态添加所有非空的可选参数
        # 这种方式更简洁且易于维护
        optional_params = {
            "seed": seed,
            "aspectRatio": aspect_ratio,
            "output_format": output_format,
            "safetyTolerance": safety_tolerance,
            "promptUpsampling": prompt_upsampling,
            "guidance": guidance_scale,
            "steps": num_inference_steps,
        }

        for key, value in optional_params.items():
            # 只有当值不是None，或者对于字符串，不是空字符串时，才添加到payload
            if value is not None and value != "":
                payload[key] = value

        print("🎨 开始生成图像...")
        # 发送请求
        try:
            response = self._make_request("POST", "/v1/draw/flux", data=payload)
        except Exception as e:
            # 确保将所有底层异常统一包装成我们的自定义异常
            if isinstance(e, GrsaiAPIError):
                raise e
            raise GrsaiAPIError(format_error_message(e, "图像生成"))

        status = response["status"]
        if status != "succeeded":
            print(f"🎨 图像生成失败: {response['id']}")
            print(json.dumps(response, indent=4, ensure_ascii=False))
            raise GrsaiAPIError(f"图像生成失败: {response['id']}")

        print("🎨 图像生成成功, 开始下载图像...")

        image_url = response["url"]
        print(image_url)
        if not isinstance(image_url, str) or not image_url.startswith("http"):
            raise GrsaiAPIError(f"API返回了无效的图片URL格式: {str(image_url)[:100]}")

        try:
            # 下载图像
            print("⬇️ 正在下载生成的图像...")
            timeout = self.config.get_config("timeout", 120)  # 提供一个默认值
            pil_image = download_image(image_url, timeout=timeout)
            if pil_image is None:
                # 这里的错误信息可以更具体
                raise GrsaiAPIError("图像下载失败，可能是网络超时或服务异常")

            print("✅ 图像生成并下载成功")
            # 直接返回PIL图像和URL，这是与之前最大的不同
            return pil_image, image_url

        except Exception as e:
            raise GrsaiAPIError(f"下载或处理图像时出错: {str(e)}")

    def test_connection(self) -> bool:
        """
        测试API连接

        Returns:
            bool: 连接是否成功
        """
        try:
            # 尝试一个简单的请求来测试连接
            self.flux_generate_image("test", seed=1)
            return True
        except:
            return False

    def get_api_status(self) -> Dict[str, Any]:
        """
        获取API状态信息

        Returns:
            Dict: 状态信息
        """
        status = {
            "api_key_valid": bool(self.config.get_api_key()),
            "base_url": self.config.get_config("api_base_url"),
            "model": self.config.get_config("model"),
            "timeout": self.config.get_config("timeout"),
        }

        # 测试连接
        try:
            status["connection_ok"] = self.test_connection()
        except:
            status["connection_ok"] = False

        return status
