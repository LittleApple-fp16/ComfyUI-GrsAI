import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(1, str(ROOT.parent.parent))

import requests
import torch
from PIL import Image

from api_client import GrsaiAPI, GrsaiAPIError
from config import GrsaiConfig
from gpt_image_nodes import ASPECT_RATIO_VIP_MAP
from minimax_nodes import GrsaiMiniMaxH3_Node, audio_to_base64, download_video
from utils import pil_to_tensor


def response(body, status=200, text=None):
    result = Mock()
    result.status_code = status
    result.ok = 200 <= status < 300
    result.text = text if text is not None else json.dumps(body)
    result.json.return_value = body
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


SUCCESS = {'id': 'task-1', 'status': 'succeeded', 'results': [{'url': 'https://example.com/image.png'}]}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.config = GrsaiConfig()
        self.config.set_config('poll_interval', 0)
        self.api = GrsaiAPI('sk-test', self.config)
        self.api.session.request = Mock(return_value=response(SUCCESS))
        self.sleep = patch('generation_api.time.sleep').start()
        self.download = patch('generation_api.download_image', return_value=Image.new('RGB', (4, 4))).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.api.close)

    def payload(self):
        return self.api.session.request.call_args.kwargs['json']

    def test_all_banana_models_and_sizes(self):
        for model in self.config.SUPPORTED_NANO_BANANA_MODELS:
            for size in ('1K', '2K', '4K'):
                with self.subTest(model=model, size=size):
                    images, urls, errors = self.api.banana_generate_image('test', model, ['base64'], '1:1', size)
                    self.assertEqual(len(images), 1)
                    self.assertFalse(errors)
                    self.assertEqual(self.payload(), dict(model=model, prompt='test', images=['base64'], aspectRatio='1:1', imageSize=size, replyType='async'))
                    self.assertTrue(self.api.session.request.call_args.args[1].endswith('/v1/api/generate'))

    def test_banana_extended_ratios_and_alias(self):
        for model in self.config.SUPPORTED_NANO_BANANA_MODELS:
            for ratio in ('1:4', '4:1', '1:8', '8:1'):
                if model.startswith('nano-banana-2'):
                    self.api.banana_generate_image('test', model, aspect_ratio=ratio)
                else:
                    with self.assertRaises(GrsaiAPIError):
                        self.api.banana_generate_image('test', model, aspect_ratio=ratio)
        self.api.banana_generate_image('test', 'nano-banana-2-cl-4k')
        self.assertEqual(self.payload()['model'], 'nano-banana-2-4k-cl')

    def test_gpt_quality_matrix(self):
        for model, allowed in self.config.GPT_IMAGE_QUALITIES.items():
            for quality in ('auto', 'low', 'medium', 'high', 'xhigh', 'max'):
                with self.subTest(model=model, quality=quality):
                    if quality in allowed:
                        self.api.gpt_image_generate_image('test', model, quality=quality)
                        self.assertEqual(self.payload()['quality'], quality)
                    else:
                        with self.assertRaises(GrsaiAPIError):
                            self.api.gpt_image_generate_image('test', model, quality=quality)

    def test_gpt_background_and_dimensions(self):
        for model in self.config.GPT_IMAGE_QUALITIES:
            if model in self.config.GPT_IMAGE_PIXEL_MODELS:
                for size in ASPECT_RATIO_VIP_MAP.values():
                    self.api.gpt_image_generate_image('test', model, size, background='transparent')
                    self.assertEqual(self.payload()['background'], 'transparent')
                for size in ('16:9', '0x1024', '1025x1024', '4096x1024', '3840x3840', '64x64', '3840x512'):
                    with self.assertRaises(GrsaiAPIError):
                        self.api.gpt_image_generate_image('test', model, size)
            else:
                for size in self.config.GPT_IMAGE_STANDARD_SIZES + ['16:9', 'auto']:
                    self.api.gpt_image_generate_image('test', model, size)
                with self.assertRaises(GrsaiAPIError):
                    self.api.gpt_image_generate_image('test', model, background='transparent')
                with self.assertRaises(GrsaiAPIError):
                    self.api.gpt_image_generate_image('test', model, '3840x2160')

    def test_async_polling_retries_only_get(self):
        self.api.session.request.side_effect = [
            response({'id': 'task /?', 'status': 'running'}),
            response({}, 429), requests.ConnectionError(), response({}, 503),
            response({'id': 'task /?', 'status': 'running'}), response(SUCCESS),
        ]
        self.api.banana_generate_image('test')
        calls = self.api.session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ['POST'] + ['GET'] * 5)
        self.assertIn('id=task+%2F%3F', calls[1].args[1])

    def test_post_failure_is_never_retried(self):
        self.api.session.request.side_effect = requests.Timeout()
        with self.assertRaises(GrsaiAPIError):
            self.api.banana_generate_image('test')
        self.assertEqual(self.api.session.request.call_count, 1)

    def test_failed_violation_and_redaction(self):
        for status in ('failed', 'violation'):
            self.api.session.request.return_value = response({'id': 'task-1', 'status': status, 'error': 'failure sk-test'}, 400)
            with self.assertRaises(GrsaiAPIError) as error:
                self.api.banana_generate_image('test')
            self.assertIn('task-1', str(error.exception))
            self.assertNotIn('sk-test', str(error.exception))

    def test_deadline(self):
        self.api.session.request.return_value = response({'id': 'task-1', 'status': 'running'})
        self.config.set_config('generation_timeout', 0)
        with self.assertRaisesRegex(GrsaiAPIError, 'task-1'):
            self.api.banana_generate_image('test')
        self.assertEqual(self.api.session.request.call_count, 1)

    def test_sse_modes(self):
        text = ': keepalive\r\n\r\nevent: update\r\ndata: {"id":"task-1","status":"running"}\r\n\r\ndata: ' + json.dumps(SUCCESS) + '\r\n\r\ndata: [DONE]\r\n\r\n'
        self.api.session.request.return_value = response(None, text=text)
        self.api.banana_generate_image('test', reply_type='stream')
        self.assertEqual(self.payload()['replyType'], 'stream')

    def test_malformed_empty_results(self):
        for body in ([], {}, {'status': 'unknown'}, {'status': 'succeeded', 'results': []}, {'status': 'succeeded', 'results': [{'url': 'file:///secret'}]}):
            self.api.session.request.return_value = response(body)
            with self.assertRaises(GrsaiAPIError):
                self.api.banana_generate_image('test')

    def test_partial_download_preserves_order(self):
        result = dict(SUCCESS, results=[{'url': 'https://example.com/first'}, {'url': 'https://example.com/fail'}, {'url': 'https://example.com/last'}])
        self.download.side_effect = lambda url, timeout: None if url.endswith('/fail') else Image.new('RGB', (2, 2))
        images, urls, errors = self.api._download_images(result)
        self.assertEqual(urls, ['https://example.com/first', 'https://example.com/last'])
        self.assertEqual(len(images), 2)
        self.assertEqual(len(errors), 1)

    def test_minimax_contract(self):
        for resolution, maximum in [('480p', 15), ('768p', 15), ('1080p', 10)]:
            self.api.minimax_generate_video('test', urls=['image'] * 9, audios=['audio'] * 3, resolution=resolution, duration=maximum)
            self.assertEqual(set(self.payload()), {'prompt', 'model', 'images', 'audios', 'aspectRatio', 'resolution', 'duration', 'seed', 'replyType'})
            with self.assertRaises(GrsaiAPIError):
                self.api.minimax_generate_video('test', resolution=resolution, duration=maximum + 1)
        for args in ({'urls': ['image'] * 10}, {'audios': ['audio'] * 4}, {'duration': 0}, {'seed': 1.5}, {'aspect_ratio': '16:9'}, {'resolution': '720p'}):
            with self.assertRaises(GrsaiAPIError):
                self.api.minimax_generate_video('test', **args)

    def test_timeout_respected(self):
        self.api.get_result('task-1', timeout=7)
        self.assertEqual(self.api.session.request.call_args.kwargs['timeout'], 7)


class NodeTests(unittest.TestCase):
    def test_all_image_node_models(self):
        modules = ['gpt_image_nodes', 'nano_banana_nodes', 'nano_banana_pro_nodes', 'nano_banana_2_nodes']
        image = Image.new('RGBA', (4, 4), (255, 0, 0, 128))
        for module_name in modules:
            module = importlib.import_module(module_name)
            for cls in module.NODE_CLASS_MAPPINGS.values():
                schema = cls.INPUT_TYPES()
                values = {}
                for name, field in {**schema['required'], **schema['optional']}.items():
                    if field[0] in ('IMAGE',):
                        continue
                    values[name] = field[1].get('default') if len(field) > 1 else field[0][0]
                values.update(prompt='test', apikey='sk-test', image_1=torch.zeros((1, 4, 4, 3)))
                for model in schema['required']['model'][0]:
                    with self.subTest(model=model), patch('generation_api.GenerationAPI.generate', return_value=SUCCESS) as generate, patch('generation_api.download_image', return_value=image):
                        values['model'] = model
                        output = cls().execute(**values)
                        self.assertEqual(output['result'][0].shape, (1, 4, 4, 4))
                        self.assertEqual(generate.call_args.args[0]['model'], model)
                        self.assertEqual(len(generate.call_args.args[0]['images']), 1)

    def test_gpt_optional_parameters_reach_api(self):
        from gpt_image_nodes import GrsaiGPTImage_Node
        with patch('generation_api.GenerationAPI.generate', return_value=SUCCESS) as generate, patch('generation_api.download_image', return_value=Image.new('RGBA', (4, 4))):
            GrsaiGPTImage_Node().execute(prompt='test', apikey='sk-test', model='gpt-image-2.5-sunburst', num_images='1', custom_size='2048x2048', quality='max', background='transparent', reply_type='json')
            payload = generate.call_args.args[0]
            self.assertEqual((payload['quality'], payload['background'], payload['aspectRatio'], payload['replyType']), ('max', 'transparent', '2048x2048', 'json'))

    def test_padding_preserves_alpha(self):
        small = Image.new('RGBA', (2, 2), (255, 0, 0, 128))
        large = Image.new('RGBA', (4, 4))
        tensor = pil_to_tensor([small, large])
        self.assertAlmostEqual(tensor[0, 1, 1, 3].item(), 128 / 255, places=6)

    def test_audio_encoding(self):
        import base64
        import wave
        encoded = audio_to_base64({'waveform': torch.zeros((1, 2, 800)), 'sample_rate': 8000})
        with wave.open(io.BytesIO(base64.b64decode(encoded))) as audio:
            self.assertEqual((audio.getnchannels(), audio.getframerate(), audio.getnframes()), (2, 8000, 800))

    def test_minimax_node(self):
        with patch('minimax_nodes.GrsaiAPI.minimax_generate_video', return_value=SUCCESS), patch('minimax_nodes.download_video', return_value='video'):
            output = GrsaiMiniMaxH3_Node().execute('test', 'sk-test', 'minimax-h3', '2', 'landscape', '768p', 10, 0)
            self.assertEqual(output['result'][0], ['video', 'video'])

    def test_video_download_and_decode(self):
        import av
        buffer = io.BytesIO()
        with av.open(buffer, mode='w', format='mp4') as output:
            stream = output.add_stream('mpeg4', rate=24)
            stream.width, stream.height = 32, 32
            stream.pix_fmt = 'yuv420p'
            frame = av.VideoFrame.from_image(Image.new('RGB', (32, 32), 'red'))
            for packet in stream.encode(frame):
                output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
        reply = response({})
        reply.iter_content.return_value = [buffer.getvalue()]
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, patch('minimax_nodes.requests.get', return_value=reply) as get:
            video = download_video('https://example.com/video.mp4', directory)
            self.assertEqual(video.get_dimensions(), (32, 32))
            self.assertNotIn('headers', get.call_args.kwargs)
            self.assertTrue(list(Path(directory).glob('*.mp4')))

    def test_failed_video_download_cleans_files(self):
        reply = response({})
        reply.iter_content.return_value = [b'invalid video']
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, patch('minimax_nodes.requests.get', return_value=reply), patch('minimax_nodes.time.sleep'):
            with self.assertRaises(GrsaiAPIError):
                download_video('https://example.com/video.mp4', directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_package_registration(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('grsai_test_package', ROOT / '__init__.py', submodule_search_locations=[str(ROOT)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = package
        spec.loader.exec_module(package)
        self.assertEqual(len(package.NODE_CLASS_MAPPINGS), 5)
        self.assertEqual(set(package.NODE_CLASS_MAPPINGS), set(package.NODE_DISPLAY_NAME_MAPPINGS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
