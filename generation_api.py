import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse

import requests

try:
    from .utils import download_image
except ImportError:
    from utils import download_image


class GrsaiAPIError(Exception):
    pass


class GenerationAPI:
    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _make_request(self, method, endpoint, data=None, timeout=None):
        url = self.config.get_config('api_base_url').rstrip('/') + endpoint
        timeout = timeout if timeout is not None else self.config.get_config('timeout', 300)
        try:
            with self.session.request(method, url, json=data, timeout=timeout) as response:
                if response.status_code == 401:
                    raise GrsaiAPIError('API密钥无效或已过期')
                if response.status_code == 429 or response.status_code >= 500:
                    error = GrsaiAPIError(f'API暂时不可用: HTTP {response.status_code}')
                    error.retryable = True
                    raise error
                try:
                    if response.text.lstrip().startswith(('data:', 'event:', ':')):
                        events = []
                        for block in response.text.replace('\r\n', '\n').split('\n\n'):
                            value = '\n'.join(line[5:].lstrip() for line in block.splitlines() if line.startswith('data:'))
                            if value and value != '[DONE]':
                                events.append(json.loads(value))
                        result = next((event for event in reversed(events) if isinstance(event, dict) and event.get('status') in ('succeeded', 'failed', 'violation')), events[-1] if events else None)
                    else:
                        result = response.json()
                except (ValueError, IndexError):
                    raise GrsaiAPIError(f'API返回无效JSON: HTTP {response.status_code}') from None
                if not isinstance(result, dict):
                    raise GrsaiAPIError('API响应必须为对象')
                if result.get('status') in ('failed', 'violation'):
                    raise GrsaiAPIError(f"任务{result['status']} ({result.get('id', 'unknown')}): {str(result.get('error', '生成失败')).replace(self.api_key, '[REDACTED]')}")
                if not response.ok or result.get('error'):
                    detail = str(result.get('error', '请求失败')).replace(self.api_key, '[REDACTED]')
                    raise GrsaiAPIError(f'API请求失败: HTTP {response.status_code} - {detail}')
                return result
        except (requests.Timeout, requests.ConnectionError):
            error = GrsaiAPIError('网络连接失败或超时')
            error.retryable = True
            raise error from None
        except requests.RequestException:
            raise GrsaiAPIError('HTTP请求失败') from None

    def get_result(self, task_id, timeout=None):
        if not isinstance(task_id, str) or not task_id.strip():
            raise GrsaiAPIError('缺少任务ID')
        return self._make_request('GET', '/v1/api/result?' + urlencode({'id': task_id}), timeout=timeout)

    def generate(self, payload):
        if not isinstance(payload.get('prompt'), str) or not payload['prompt'].strip():
            raise GrsaiAPIError('提示词不能为空')
        if payload.get('replyType') not in ('json', 'async', 'stream'):
            raise GrsaiAPIError('replyType 必须为 json、async 或 stream')
        deadline = time.monotonic() + self.config.get_config('generation_timeout', 1800)
        response = self._make_request('POST', '/v1/api/generate', data=payload,
                                      timeout=min(self.config.get_config('timeout', 300),
                                                  max(0.001, deadline - time.monotonic())))
        task_id = response.get('id')
        delay = self.config.get_config('poll_interval', 3)
        while response.get('status') == 'running':
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GrsaiAPIError(f'等待生成超时，任务ID: {task_id}，可查询原任务')
            time.sleep(min(delay, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                continue
            try:
                response = self.get_result(task_id, timeout=min(self.config.get_config('timeout', 300), remaining))
                delay = self.config.get_config('poll_interval', 3)
            except GrsaiAPIError as exc:
                if not getattr(exc, 'retryable', False):
                    raise GrsaiAPIError(f'{exc}，任务ID: {task_id}') from None
                delay = min(max(delay * 2, 1), 30)
        if response.get('status') != 'succeeded':
            raise GrsaiAPIError(f"任务状态异常: {response.get('status')}，任务ID: {task_id}")
        results = response.get('results')
        if not isinstance(results, list) or not results:
            raise GrsaiAPIError('生成成功但未返回结果')
        for result in results:
            url = result.get('url') if isinstance(result, dict) else None
            if not isinstance(url, str) or urlparse(url).scheme not in ('http', 'https') or not urlparse(url).netloc:
                raise GrsaiAPIError('生成结果URL无效')
        return response

    def _download_images(self, response):
        urls = [result['url'] for result in response['results']]
        def download(url):
            for attempt in range(3):
                try:
                    image = download_image(url, timeout=self.config.get_config('timeout', 300))
                    if image is not None:
                        image.load()
                        return image
                except Exception:
                    pass
                if attempt < 2:
                    time.sleep(1 + attempt)
            return None
        with ThreadPoolExecutor(max_workers=min(len(urls), 8)) as executor:
            images = list(executor.map(download, urls))
        return ([image for image in images if image is not None],
                [url for image, url in zip(images, urls) if image is not None],
                [f'图像下载失败，可重试原链接: {url}' for image, url in zip(images, urls) if image is None])

    @staticmethod
    def _references(values, limit=None):
        if values is None:
            return []
        if not isinstance(values, (list, tuple)) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise GrsaiAPIError('参考素材必须为非空 base64 或 URL 字符串数组')
        if limit is not None and len(values) > limit:
            raise GrsaiAPIError(f'参考素材最多支持 {limit} 个')
        return list(values)

    def banana_generate_image(self, prompt, model='nano-banana-fast', urls=None,
                              aspect_ratio=None, image_size=None, reply_type='async'):
        model = {'nano-banana-2-cl-4k': 'nano-banana-2-4k-cl'}.get(model, model)
        if model not in self.config.SUPPORTED_NANO_BANANA_MODELS:
            raise GrsaiAPIError(f'不支持的模型: {model}')
        payload = dict(model=model, prompt=prompt, images=self._references(urls), replyType=reply_type)
        if aspect_ratio:
            if not self.config.validate_nano_banana_aspect_ratio(aspect_ratio, model):
                raise GrsaiAPIError(f'{model} 不支持宽高比 {aspect_ratio}')
            payload['aspectRatio'] = aspect_ratio
        if image_size:
            if not self.config.validate_nano_banana_image_size(image_size):
                raise GrsaiAPIError(f'不支持的 imageSize: {image_size}')
            payload['imageSize'] = image_size
        return self._download_images(self.generate(payload))

    def gpt_image_generate_image(self, prompt, model='gpt-image-2', aspect_ratio=None,
                                 urls=None, quality=None, background=None, reply_type='async'):
        qualities = self.config.GPT_IMAGE_QUALITIES
        if model not in qualities:
            raise GrsaiAPIError(f'不支持的模型: {model}')
        payload = dict(model=model, prompt=prompt, images=self._references(urls), replyType=reply_type)
        if quality not in (None, '', 'default'):
            if quality not in qualities[model]:
                raise GrsaiAPIError(f"{model} 的 quality 支持: {', '.join(qualities[model])}")
            payload['quality'] = quality
        if background not in (None, '', 'default'):
            if model not in self.config.GPT_IMAGE_PIXEL_MODELS or background != 'transparent':
                raise GrsaiAPIError(f'{model} 不支持 background={background}')
            payload['background'] = background
        if aspect_ratio:
            if model in self.config.GPT_IMAGE_PIXEL_MODELS and aspect_ratio != 'auto':
                match = re.fullmatch(r'(\d+)x(\d+)', aspect_ratio)
                if not match:
                    raise GrsaiAPIError(f'{model} 需要像素尺寸，如 1024x1024')
                width, height = map(int, match.groups())
                if min(width, height) <= 0 or max(width, height) > 3840 or width % 16 or height % 16 or max(width, height) > 3 * min(width, height) or not 655360 <= width * height <= 8294400:
                    raise GrsaiAPIError('尺寸必须符合16像素对齐、最大边3840、比例不超过3:1和总像素655360~8294400')
            elif model not in self.config.GPT_IMAGE_PIXEL_MODELS:
                if aspect_ratio != 'auto' and aspect_ratio not in self.config.GPT_IMAGE_STANDARD_SIZES and not re.fullmatch(r'[1-9]\d*:[1-9]\d*', aspect_ratio):
                    raise GrsaiAPIError('标准模型需要比例或文档列出的1K像素尺寸')
            payload['aspectRatio'] = aspect_ratio
        return self._download_images(self.generate(payload))

    def minimax_generate_video(self, prompt, model='minimax-h3', urls=None, audios=None,
                               aspect_ratio='landscape', resolution='768p', duration=10,
                               seed=0, reply_type='async'):
        if model != 'minimax-h3':
            raise GrsaiAPIError(f'不支持的模型: {model}')
        if aspect_ratio not in ('portrait', 'landscape', 'square'):
            raise GrsaiAPIError('视频比例必须为 portrait、landscape 或 square')
        if resolution not in ('480p', '768p', '1080p'):
            raise GrsaiAPIError('视频分辨率必须为 480p、768p 或 1080p')
        if type(duration) is not int or not 1 <= duration <= (10 if resolution == '1080p' else 15):
            raise GrsaiAPIError('视频时长支持1~15秒，1080p最多10秒')
        if type(seed) is not int:
            raise GrsaiAPIError('seed 必须为整数')
        return self.generate(dict(model=model, prompt=prompt, images=self._references(urls, 9),
                                  audios=self._references(audios, 3), aspectRatio=aspect_ratio,
                                  resolution=resolution, duration=duration, seed=seed, replyType=reply_type))
