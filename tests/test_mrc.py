import os.path

import numpy as np
import torch
import pytest
from cryodrgn import dataset, mrc
from cryodrgn.source import ImageSource

DATA_FOLDER = os.path.join(os.path.dirname(__file__), "..", "testing", "data")


@pytest.fixture
def mrcs_data():
    return ImageSource.from_mrcs(
        f"{DATA_FOLDER}/toy_projections.mrcs", lazy=False
    ).images()


def test_lazy_loading(mrcs_data):
    data = ImageSource.from_mrcs(
        f"{DATA_FOLDER}/toy_projections.mrcs", lazy=True
    ).images()
    assert isinstance(data, torch.Tensor)
    assert np.allclose(data, mrcs_data)


def test_star(mrcs_data):
    data = ImageSource.from_star(f"{DATA_FOLDER}/toy_projections.star").images()
    assert isinstance(data, torch.Tensor)
    assert np.allclose(data, mrcs_data)


def test_txt(mrcs_data):
    data = ImageSource.from_txt(f"{DATA_FOLDER}/toy_projections.txt").images()
    assert isinstance(data, torch.Tensor)
    assert np.allclose(data, mrcs_data)
