# Retained Training results and Validation

A previously anchored successful Training result can be reviewed for held-out
Validation after an application update. `core/training_retained_result.py`
compares the recorded plan, contract, stopped journal, three input-verification
receipts, log and final-result anchor. It inspects the current package,
checkpoint and output and reconstructs the original receipt; the caller must
supply the saved expected receipt hash. Missing or changed proof is an error.

Historical verification does not reread old application executables or rescan
the old software environment. It grants no Training launch, resume, anchor
creation or registration authority. Current Training launch owners and their
qualification remain unchanged. Schema changes need their own supported reader;
this does not promise arbitrary future-format migration.

Validation then captures its own current numerical worker, read-side owner,
resources and software inputs through the existing preparation/process services.
Its measurements are denoising MSE and do not establish image quality.

The exact source/build and generated native evidence remain separate from a
published update. The Manager package must include this reader and its declared
code identity before delivery.

Public regression coverage: `tests/test_training_retained_result.py`. The bounded
production reader additionally reproduced the exact saved native result receipt;
current evaluator qualification includes the read-side owner code identity.
