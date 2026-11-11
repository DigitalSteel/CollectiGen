# Import an unlisted Validation review

Manager Training History now offers **Import Validation Review**. Select the existing saved Validation receipt, review its checked measurement, and find it in Training History afterward. This uses the canonical retained-review dialog and scoped History owner; it does not start training or a new evaluation. Cancelling the picker leaves History unchanged. A Manager session that ends while the picker is open cannot open or index the receipt.

Owner: TRN-09. Reference: all-in-one commit `0b15c381`, Training `Import Validation Review` -> `TrainingWorkflowController.open_validation`. This restores a missing modular entry rather than adding a new evaluator.

Delivery remains open under the existing Manager 0.6 candidate. Source checks do not qualify an installed or published update. Numerical Training/evaluator evidence is reused unchanged; this patch changes only receipt selection/routing.
