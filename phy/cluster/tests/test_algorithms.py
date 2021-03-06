# -*- coding: utf-8 -*-

"""Tests of clustering algorithms."""

#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import os.path as op

import numpy as np
from numpy.testing import assert_equal as ae
from pytest import fixture

from ...utils._misc import _read_python
from ...utils.datasets import _download_test_data
from ...utils.logging import set_level
from ...utils.tempdir import TemporaryDirectory
from ...utils.settings import Settings
from ...io.kwik import KwikModel
from ...io.kwik.mock import create_mock_kwik
from ...io.mock import artificial_traces
from ..algorithms import cluster, SpikeDetekt, _split_spikes, _concat


#------------------------------------------------------------------------------
# Fixtures
#------------------------------------------------------------------------------

def setup():
    set_level('debug')


def teardown():
    set_level('info')


sample_rate = 10000
n_samples = 25000
n_channels = 4


def _spikedetekt(request, n_groups=2):
    tmpdir = TemporaryDirectory()

    traces = artificial_traces(n_samples, n_channels)
    traces[5000:5010, 1] *= 5
    traces[15000:15010, 3] *= 5

    # Load default settings.
    curdir = op.dirname(op.realpath(__file__))
    default_settings_path = op.join(curdir, '../default_settings.py')
    settings = _read_python(default_settings_path)
    params = settings['spikedetekt_params'](sample_rate)
    params['sample_rate'] = sample_rate

    if n_groups == 1:
        params['probe_adjacency_list'] = {0: [1, 2],
                                          1: [0, 2],
                                          2: [0, 1],
                                          3: []}
        params['probe_channels'] = {0: [0, 1, 2, 3]}
    elif n_groups == 2:
        params['probe_adjacency_list'] = {0: [1, 2],
                                          1: [0, 2],
                                          2: [0, 1],
                                          3: []}
        params['probe_channels'] = {0: [0, 1, 2], 1: [3]}

    sd = SpikeDetekt(tempdir=tmpdir.name, **params)

    def end():
        tmpdir.cleanup()
    request.addfinalizer(end)

    return sd, traces, params


@fixture
def spikedetekt(request):
    return _spikedetekt(request)


@fixture
def spikedetekt_one_group(request):
    return _spikedetekt(request, n_groups=1)


#------------------------------------------------------------------------------
# Tests spike detection
#------------------------------------------------------------------------------

def test_split_spikes():
    groups = np.zeros(10, dtype=np.int)
    groups[1::2] = 1

    idx = np.ones(10, dtype=np.bool)
    idx[0] = False
    idx[-1] = False

    a = np.random.rand(10, 2)
    b = np.random.rand(10, 3, 2)

    out = _split_spikes(groups, idx, a=a, b=b)

    assert sorted(out) == [0, 1]
    assert sorted(out[0]) == ['a', 'b']
    assert sorted(out[1]) == ['a', 'b']

    ae(out[0]['a'], a[1:-1][1::2])
    ae(out[0]['b'], b[1:-1][1::2])

    ae(out[1]['a'], a[1:-1][::2])
    ae(out[1]['b'], b[1:-1][::2])


def test_spike_detect_methods(spikedetekt_one_group):
    sd, traces, params = spikedetekt_one_group

    # Filter the data.
    traces_f = sd.apply_filter(traces)
    assert traces_f.shape == traces.shape
    assert not np.any(np.isnan(traces_f))

    # Thresholds.
    thresholds = sd.find_thresholds(traces)
    assert 0 < thresholds['weak'] < thresholds['strong']

    # Spike detection.
    traces_f[1000:1010, :3] *= 5
    traces_f[2000:2010, [0, 2]] *= 5
    traces_f[3000:3020, :] *= 5
    components = sd.detect(traces_f, thresholds)
    assert isinstance(components, list)
    n_spikes = len(components)
    n_samples_waveforms = (params['extract_s_before'] +
                           params['extract_s_after'])

    # Spike extraction.
    groups, samples, waveforms, masks = sd.extract_spikes(components,
                                                          traces_f,
                                                          thresholds,
                                                          )

    waveforms = _concat(waveforms, np.float32)
    masks = _concat(masks, np.float32)

    assert np.all(np.in1d(groups, [0, 1]))
    assert samples.dtype == np.float64
    assert samples.shape == (n_spikes,)
    assert waveforms.shape == (n_spikes, n_samples_waveforms, n_channels)
    assert masks.shape == (n_spikes, n_channels)
    assert 0. <= masks.min() < masks.max() <= 1.
    assert not np.any(np.isnan(samples))
    assert not np.any(np.isnan(waveforms))
    assert not np.any(np.isnan(masks))

    # PCA.
    pcs = sd.waveform_pcs(waveforms, masks)
    n_pcs = params['nfeatures_per_channel']
    assert pcs.shape == (n_pcs, n_samples_waveforms, n_channels)
    assert not np.any(np.isnan(pcs))

    # Features.
    features = sd.features(waveforms, pcs)
    assert features.shape == (n_spikes, n_channels, n_pcs)
    assert not np.any(np.isnan(features))


def test_spike_detect_serial(spikedetekt):
    sd, traces, params = spikedetekt
    out = sd.run_serial(traces)

    n_samples_waveforms = (params['extract_s_before'] +
                           params['extract_s_after'])
    n_features = params['nfeatures_per_channel']

    assert out.n_spikes_total >= 0
    assert sum(out.n_spikes_per_group.values()) == out.n_spikes_total
    assert len(out.chunk_keys) == 3

    traces_f = np.vstack(out.traces_f)
    assert traces_f.shape == (n_samples, n_channels)
    assert traces_f.dtype == np.float32

    for group in [0, 1]:
        # Number of channels in the group.
        n_channels_g = (3, 1)[group]
        n_spikes_g = out.n_spikes_per_group[group]

        waveforms = np.vstack(out.waveforms[group])
        assert waveforms.dtype == np.float32
        assert waveforms.shape == (n_spikes_g,
                                   n_samples_waveforms,
                                   n_channels_g)

        features = np.vstack(out.features[group])
        assert features.dtype == np.float32
        assert features.shape == (n_spikes_g, n_channels_g, n_features)

        masks = np.vstack(out.masks[group])
        assert masks.dtype == np.float32
        assert masks.shape == (n_spikes_g, n_channels_g)


def test_spike_detect_real_data(spikedetekt):
    with TemporaryDirectory() as tempdir:

        # Set the parameters.
        curdir = op.dirname(op.realpath(__file__))
        default_settings_path = op.join(curdir, '../default_settings.py')
        settings = Settings(default_path=default_settings_path)
        sample_rate = 20000
        params = settings['spikedetekt_params'](sample_rate)
        params['sample_rate'] = sample_rate
        params['probe_adjacency_list'] = {0: [1, 2, 3],
                                          1: [0, 2, 3],
                                          2: [0, 1, 3],
                                          3: [0, 1, 2]}
        params['probe_channels'] = {0: [0, 1, 2, 3]}

        # Load the traces.
        path = _download_test_data('test.dat')
        traces = np.fromfile(path, dtype=np.int16).reshape((20000, 4))

        # Run the detection.
        sd = SpikeDetekt(tempdir=tempdir, **params)
        out = sd.run_serial(traces)

        n_spikes = 49
        assert out.n_spikes_total == n_spikes
        spike_samples = out.spike_samples[0][0][...]
        assert spike_samples.shape == (n_spikes,)

        masks = out.masks[0][0][...]
        assert masks.shape == (n_spikes, n_channels)

        # Plot...
        # from phy.gui import qt_app
        # from phy.plot.traces import plot_traces
        # with qt_app():
        #     plot_traces(traces,
        #                 spike_samples=spike_samples,
        #                 masks=masks,
        #                 n_samples_per_spike=20,
        #                 )


#------------------------------------------------------------------------------
# Tests clustering
#------------------------------------------------------------------------------

def test_cluster():
    n_spikes = 100
    with TemporaryDirectory() as tempdir:
        filename = create_mock_kwik(tempdir,
                                    n_clusters=1,
                                    n_spikes=n_spikes,
                                    n_channels=8,
                                    n_features_per_channel=3,
                                    n_samples_traces=5000)
        model = KwikModel(filename)

        spike_clusters = cluster(model, num_starting_clusters=10)
        assert len(spike_clusters) == n_spikes

        spike_clusters = cluster(model, num_starting_clusters=10,
                                 spike_ids=range(100))
        assert len(spike_clusters) == 100
