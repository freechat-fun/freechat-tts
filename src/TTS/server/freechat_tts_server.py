#!flask/bin/python
import argparse
import io
import os
import sys
import time
from pathlib import Path
from threading import Lock
from typing import List

import numpy as np
import torch
import torchaudio
from flask import Flask, render_template, request, jsonify, Response, send_file

from TTS.config import load_config
from TTS.tts.configs.xtts_config import XttsConfig
from TTS.tts.models.xtts import Xtts
from TTS.utils.audio.numpy_transforms import save_wav
from TTS.utils.generic_utils import get_user_data_dir
from TTS.utils.manage import ModelManager


def create_argparser():
    def convert_boolean(x):
        return x.lower() in ['true', '1', 'yes']

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--list_models',
        type=convert_boolean,
        nargs='?',
        const=True,
        default=False,
        help='list available pre-trained tts and vocoder models.',
    )
    parser.add_argument(
        '--model_name',
        type=str,
        default='tts_models/en/ljspeech/tacotron2-DDC',
        help='Name of one of the pre-trained tts models in format <language>/<dataset>/<model_name>',
    )
    parser.add_argument('--vocoder_name', type=str, default=None, help='name of one of the released vocoder models.')

    # Args for running custom models
    parser.add_argument('--config_path', default=None, type=str, help='Path to model config file.')
    parser.add_argument(
        '--model_path',
        type=str,
        default=None,
        help='Path to model file.',
    )
    parser.add_argument(
        '--vocoder_path',
        type=str,
        help='Path to vocoder model file. If it is not defined, model uses GL as vocoder. Please make sure that you installed vocoder library before (WaveRNN).',
        default=None,
    )
    parser.add_argument("--vocoder_config_path", type=str, help="Path to vocoder model config file.", default=None)
    parser.add_argument('--speakers_file_path', type=str, help='JSON file for multi-speaker model.', default=None)
    parser.add_argument('--vocab_path', type=str, help='Path to vocab file.', default=None)
    parser.add_argument('--port', type=int, default=5002, help='port to listen on.')
    parser.add_argument('--use_cuda', type=convert_boolean, default=False, help='true to use CUDA.')
    parser.add_argument('--debug', type=convert_boolean, default=False, help='true to enable Flask debug mode.')
    parser.add_argument('--show_details', type=convert_boolean, default=False, help='Generate model detail page.')
    return parser


# parse the args
args = create_argparser().parse_args()

path = Path(__file__).parent / '../.models.json'
manager = ModelManager(path)

# update in-use models to the specified released models.
model_path = None
config_path = None
speakers_file_path = None
vocab_path = None
vocoder_path = None
vocoder_config_path = None

# CASE1: list pre-trained TTS models
if args.list_models:
    manager.list_models()
    sys.exit()

# CASE2: load pre-trained model paths
if args.model_name is not None and not args.model_path:
    model_path, config_path, model_item = manager.download_model(args.model_name)
    args.vocoder_name = model_item['default_vocoder'] if args.vocoder_name is None else args.vocoder_name

if args.vocoder_name is not None and not args.vocoder_path:
    vocoder_path, vocoder_config_path, _ = manager.download_model(args.vocoder_name)

# CASE3: set custom model paths
if args.model_path is not None:
    model_path = args.model_path
    config_path = args.config_path
    speakers_file_path = args.speakers_file_path
    vocab_path = args.vocab_path

# create the xtts model
config = XttsConfig()
config.load_json(config_path)
model = Xtts.init_from_config(config)
if args.use_cuda:
    model.load_checkpoint(config, checkpoint_dir=model_path, vocab_path=vocab_path, use_deepspeed=True)
    model.cuda()
else:
    model.load_checkpoint(config, checkpoint_dir=model_path, vocab_path=vocab_path)


def to_bytes(wav: List[int] | torch.Tensor) -> io.BytesIO:
    out = io.BytesIO()
    if torch.is_tensor(wav):
        wav = wav.cpu().numpy()
    if isinstance(wav, list):
        wav = np.array(wav)
    save_wav(wav=wav, path=out, sample_rate=config.model_args.output_sample_rate, pipe_out=None)
    return out


def get_work_data_dir(module: str) -> str:
    data_home = os.environ.get("DATA_HOME")
    return os.path.join(data_home, module) if data_home else get_user_data_dir(module)


# APIs
app = Flask(__name__)
lock = Lock()


@app.route('/inference', methods=['GET', 'POST'])
def tts():
    with lock:
        text = request.headers.get('text') or request.values.get('text', '')
        speaker_idx = request.headers.get('speaker-id') or request.values.get('speaker_id', '')
        language_idx = request.headers.get('language-id') or request.values.get('language_id', '')
        speaker_wav = request.headers.get('speaker-wav') or request.values.get('speaker_wav', '')

        print(f' > Model input: {text}')
        print(f' > Language Idx: {language_idx}')
        if speaker_idx:
            print(f' > Speaker Idx: {speaker_idx}')
            gpt_cond_latent, speaker_embedding = model.speaker_manager.speakers[speaker_idx].values()
        elif speaker_wav:
            print(f' > Speaker Wav: {speaker_wav}')
            gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(audio_path=[speaker_wav])
        else:
            gpt_cond_latent, speaker_embedding = None, None

        result = model.inference(text, language_idx, gpt_cond_latent, speaker_embedding)
        out = to_bytes(result['wav'])
        return Response(out, mimetype='audio/wav', direct_passthrough=True)


@app.route('/inference/stream', methods=['GET', 'POST'])
def tts_stream():
    with lock:
        text = request.headers.get('text') or request.values.get('text', '')
        speaker_idx = request.headers.get('speaker-id') or request.values.get('speaker_id', '')
        language_idx = request.headers.get('language-id') or request.values.get('language_id', '')
        speaker_wav = request.headers.get('speaker-wav') or request.values.get('speaker_wav', '')

        print(f' > Model input: {text}')
        print(f' > Language Idx: {language_idx}')
        if speaker_idx:
            print(f' > Speaker Idx: {speaker_idx}')
            gpt_cond_latent, speaker_embedding = model.speaker_manager.speakers[speaker_idx].values()
        elif speaker_wav:
            print(f' > Speaker Wav: {speaker_wav}')
            gpt_cond_latent, speaker_embedding = model.get_conditioning_latents(audio_path=[speaker_wav])
        else:
            gpt_cond_latent, speaker_embedding = None, None

        def generate_chunks():
            print('Inference...')
            t0 = time.time()

            chunks = model.inference_stream(text, language_idx, gpt_cond_latent, speaker_embedding)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    print(f'Time to first chunk: {time.time() - t0}')
                print(f'Received chunk {i} of audio length {chunk.shape[-1]}')
                yield to_bytes(chunk)

        return Response(generate_chunks(), mimetype='audio/wav', direct_passthrough=True)


@app.route('/speaker/wav', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']

    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    upload_folder = get_work_data_dir('wav')
    file_path = os.path.join(upload_folder, file.filename)
    file.save(file_path)

    return jsonify({'message': f'File {file.filename} uploaded successfully!'}), 200


@app.route('/speaker/wav/<filename>', methods=['DELETE'])
def delete_file(filename):
    upload_folder = get_work_data_dir('wav')
    file_path = os.path.join(upload_folder, filename)

    try:
        os.remove(file_path)
        return jsonify({'message': f'File {filename} deleted successfully!'}), 200
    except FileNotFoundError:
        return jsonify({'error': 'File not found'}), 404


@app.route('/details')
def details():
    if args.config_path is not None and os.path.isfile(args.config_path):
        model_config = load_config(args.config_path)
    else:
        if args.model_name is not None:
            model_config = load_config(config_path)
        else:
            model_config = None

    if args.vocoder_config_path is not None and os.path.isfile(args.vocoder_config_path):
        vocoder_config = load_config(args.vocoder_config_path)
    else:
        if args.vocoder_name is not None:
            vocoder_config = load_config(vocoder_config_path)
        else:
            vocoder_config = None

    return render_template(
        'details.html',
        show_details=args.show_details,
        model_config=model_config,
        vocoder_config=vocoder_config,
        args=args.__dict__,
    )


@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200


def test_stream():
    gpt_cond_latent, speaker_embedding = model.speaker_manager.speakers['Claribel Dervla'].values()

    print("Inference...")
    t0 = time.time()
    chunks = model.inference_stream(
        "It took me quite a long time to develop a voice and now that I have it I am not going to be silent.",
        "en",
        gpt_cond_latent,
        speaker_embedding
    )

    wav_chunks = []
    for i, chunk in enumerate(chunks):
        if i == 0:
            print(f"Time to first chunk: {time.time() - t0}")
        print(f"Received chunk {i} of audio length {chunk.shape[-1]}")
        wav_chunks.append(chunk)
    wav = torch.cat(wav_chunks, dim=0)
    torchaudio.save("xtts_streaming.wav", wav.squeeze().unsqueeze(0).cpu(), 24000)


def main():
    app.run(debug=args.debug, host='::', port=args.port)


if __name__ == '__main__':
    main()