"""Load the delivered Paddle weights into the matching PP-OCRv5 ONNX graph."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
import paddle
import yaml
from onnx import numpy_helper
from PIL import Image


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sample_input(path):
    image = Image.open(path).convert('RGB')
    width = min(320, int(np.ceil(image.width * 48 / image.height)))
    resized = np.asarray(image.resize((width, 48), Image.Resampling.BILINEAR))
    data = np.zeros((1, 3, 48, 320), dtype=np.float32)
    data[0, :, :, :width] = resized[:, :, ::-1].transpose(2, 0, 1) / 127.5 - 1
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('delivery', type=Path)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    source = args.delivery / 'model'
    structure = json.loads((source / 'inference.json').read_text())
    names = {op['A'][3] for region in structure['program']['regions']
             for block in region['blocks'] for op in block['ops'] if op['#'] == 'p'}

    paddle.enable_static()
    executor = paddle.static.Executor(paddle.CPUPlace())
    program, feeds, fetches = paddle.static.load_inference_model(str(source / 'inference'), executor)
    scope = paddle.static.global_scope()
    weights = {name: np.array(scope.find_var(name).get_tensor()) for name in names}
    model = onnx.load(args.baseline)
    producers = {output: node for node in model.graph.node for output in node.output}
    consumers = {value: node for node in model.graph.node for value in node.input}
    character_list = yaml.safe_load((source / 'inference.yml').read_text())['PostProcess']['character_dict']
    dictionary = (source / 'ppocrv5_dict.txt').read_text().removesuffix('\n').split('\n')
    assert dictionary == character_list
    assert next(p.value for p in model.metadata_props if p.key == 'character').splitlines() == dictionary

    mapped = set()
    for tensor in model.graph.initializer:
        source_name = tensor.name
        if tensor.name.startswith('p2o.pd_op.reshape.') and tensor.name not in weights:
            bias_add = consumers[tensor.name]
            conv = producers[bias_add.input[0]]
            assert bias_add.op_type == 'Add' and conv.op_type == 'Conv'
            source_name = conv.input[1].removesuffix('.w_0') + '.b_0'
        elif tensor.name.startswith('helper.reshape.'):
            index = int(tensor.name.rsplit('.', 1)[1])
            source_name = f'layer_norm_{index // 2}.{"b" if index % 2 else "w"}_0'
        if source_name in weights:
            old = numpy_helper.to_array(tensor)
            value = weights[source_name].reshape(old.shape)
            assert value.dtype == old.dtype and np.isfinite(value).all()
            tensor.CopyFrom(numpy_helper.from_array(value, tensor.name))
            mapped.add(source_name)
    assert mapped == names, sorted(names - mapped)

    paddle_hash = sha256(source / 'inference.pdiparams')
    provenance = model.metadata_props.add()
    provenance.key = 'source_paddle_weights_sha256'
    provenance.value = paddle_hash
    onnx.checker.check_model(model)
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(model.SerializeToString(), options, providers=['CPUExecutionProvider'])
    rng = np.random.default_rng(20261112)
    checks = [(f'input_{batch}x{width}', rng.uniform(-1, 1, (batch, 3, 48, width)).astype(np.float32))
              for batch, width in [(1, 160), (2, 320), (1, 672), (1, 1024)]]
    checks.extend((path.name, sample_input(path)) for path in sorted((args.delivery / 'samples').glob('*.png')))
    report = []
    for label, image in checks:
        expected = executor.run(program, feed={feeds[0]: image}, fetch_list=fetches)[0]
        actual = session.run(None, {session.get_inputs()[0].name: image})[0]
        assert actual.shape[-1] == len(dictionary) + 2
        np.testing.assert_allclose(actual, expected, atol=2e-4, rtol=2e-4, err_msg=label)
        assert np.array_equal(actual.argmax(axis=-1), expected.argmax(axis=-1)), label
        report.append({'input': label, 'shape': list(actual.shape),
                       'max_probability_difference': float(np.max(np.abs(actual - expected)))})

    onnx.save(model, args.output)
    evidence = {'onnx_sha256': sha256(args.output), 'paddle_weights_sha256': paddle_hash,
                'baseline_graph_sha256': sha256(args.baseline), 'parameters_mapped': len(mapped),
                'dictionary_entries': len(dictionary), 'validation': report}
    args.output.with_suffix('.validation.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    main()
