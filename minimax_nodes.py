import base64
import io
import json
import os
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

try:
    from .api_client import GrsaiAPI, GrsaiAPIError
    from .config import default_config
    from .utils import tensor_to_base64
except ImportError:
    from api_client import GrsaiAPI, GrsaiAPIError
    from config import default_config
    from utils import tensor_to_base64


def download_video(url, directory, timeout=300):
    Path(directory).mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, suffix='.mp4', delete=False) as output:
                path = output.name
                with requests.get(url, stream=True, timeout=timeout) as response:
                    response.raise_for_status()
                    for chunk in response.iter_content(1024 * 1024):
                        output.write(chunk)
            if not os.path.getsize(path):
                raise ValueError('空视频文件')
            from comfy_api.input_impl import VideoFromFile
            video = VideoFromFile(path)
            video.get_dimensions()
            return video
        except Exception:
            if path and os.path.exists(path):
                os.unlink(path)
            if attempt == 2:
                raise GrsaiAPIError(f'视频下载或解码失败，可重试原链接: {url}') from None
            time.sleep(attempt + 1)


def audio_to_base64(audio):
    waveform = audio['waveform']
    if waveform.ndim != 3 or waveform.shape[0] != 1 or waveform.shape[1] not in (1, 2):
        raise GrsaiAPIError('每个音频输入需要一段单声道或双声道音频')
    samples = (waveform[0].detach().cpu().clamp(-1, 1).T.numpy() * 32767).astype('<i2')
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setnchannels(waveform.shape[1])
        output.setsampwidth(2)
        output.setframerate(int(audio['sample_rate']))
        output.writeframes(samples.tobytes())
    return base64.b64encode(buffer.getvalue()).decode('ascii')


class GrsaiMiniMaxH3_Node:
    FUNCTION = 'execute'
    CATEGORY = 'GrsAI/MiniMax'
    RETURN_TYPES = ('VIDEO', 'STRING')
    RETURN_NAMES = ('video', 'status')
    OUTPUT_IS_LIST = (True, False)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            'required': {
                'prompt': ('STRING', {'multiline': True, 'default': ''}),
                'apikey': ('STRING', {'default': ''}),
                'model': (['minimax-h3'],),
                'num_videos': ([str(i) for i in range(1, 13)], {'default': '1'}),
                'aspect_ratio': (['portrait', 'landscape', 'square'], {'default': 'landscape'}),
                'resolution': (['480p', '768p', '1080p'], {'default': '768p'}),
                'duration': ('INT', {'default': 10, 'min': 1, 'max': 15}),
                'seed': ('INT', {'default': 0, 'min': 0, 'max': 0xFFFFFFFFFFFFFFFF}),
            },
            'optional': {
                'reply_type': (['async', 'json', 'stream'], {'default': 'async'}),
                'images': ('STRING', {'default': '[]', 'multiline': True, 'tooltip': '参考图片 URL/base64 的 JSON 数组，与图片输入合计最多9张'}),
                'audios': ('STRING', {'default': '[]', 'multiline': True, 'tooltip': '参考音频 URL/base64 的 JSON 数组，与音频输入合计最多3段'}),
                **{f'image_{i}': ('IMAGE',) for i in range(1, 10)},
                **{f'audio_{i}': ('AUDIO',) for i in range(1, 4)},
            },
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float('NaN')

    def _create_error_result(self, message):
        status = f'失败: {message}'
        return {'ui': {'string': [status]}, 'result': ([], status)}

    def execute(self, prompt, apikey, model, num_videos, aspect_ratio, resolution,
                duration, seed, reply_type='async', images='[]', audios='[]', **kwargs):
        try:
            import folder_paths
            from comfy_api.input_impl import VideoFromFile
            count = int(num_videos)
            if not 1 <= count <= 12:
                raise GrsaiAPIError('批量数量支持1~12')
            image_refs = GrsaiAPI._references(json.loads(images), 9)
            audio_refs = GrsaiAPI._references(json.loads(audios), 3)
            for i in range(1, 10):
                image = kwargs.get(f'image_{i}')
                if image is not None:
                    for frame in image:
                        image_refs.append(tensor_to_base64(frame.unsqueeze(0)))
            for i in range(1, 4):
                if kwargs.get(f'audio_{i}') is not None:
                    audio_refs.append(audio_to_base64(kwargs[f'audio_{i}']))
            GrsaiAPI._references(image_refs, 9)
            GrsaiAPI._references(audio_refs, 3)
            key = apikey.strip() or default_config.get_api_key()
            output_dir = Path(folder_paths.get_output_directory()) / 'GrsAI'
            def generate_one():
                with GrsaiAPI(key) as client:
                    response = client.minimax_generate_video(
                        prompt, model, image_refs, audio_refs, aspect_ratio,
                        resolution, duration, seed, reply_type)
                    videos, errors = [], []
                    for result in response['results']:
                        try:
                            videos.append(download_video(result['url'], output_dir,
                                                         client.config.get_config('timeout', 300)))
                        except GrsaiAPIError as exc:
                            errors.append(str(exc))
                    return videos, errors
            videos, errors = [], []
            with ThreadPoolExecutor(max_workers=count) as executor:
                futures = [executor.submit(generate_one) for _ in range(count)]
                for future in as_completed(futures):
                    try:
                        generated, failures = future.result()
                        videos.extend(generated)
                        errors.extend(failures)
                    except Exception as exc:
                        errors.append(str(exc))
            if not videos:
                return self._create_error_result('; '.join(errors))
            status = f'MiniMax H3 | {resolution} | {duration}秒 | 成功: {len(videos)}'
            if errors:
                status += ' | ' + '; '.join(errors)
            return {'ui': {'string': [status]}, 'result': (videos, status)}
        except Exception as exc:
            return self._create_error_result(str(exc))


NODE_CLASS_MAPPINGS = {'Grsai_MiniMaxH3': GrsaiMiniMaxH3_Node}
NODE_DISPLAY_NAME_MAPPINGS = {'Grsai_MiniMaxH3': '🎬 GrsAI MiniMax H3 - Text/Image/Audio'}
