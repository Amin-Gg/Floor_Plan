"""Ground-truth based evaluation for floor-plan detection and downstream impact."""

from .dataset import DatasetContractError, EvaluationDataset, load_dataset
from .metrics import EvaluationConfig, evaluate_dataset

__all__ = [
    "DatasetContractError",
    "EvaluationConfig",
    "EvaluationDataset",
    "evaluate_dataset",
    "load_dataset",
]
