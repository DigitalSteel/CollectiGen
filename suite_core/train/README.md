# CollectiGen Train 0.2.0-alpha.1

Train is the Suite's separate application for reviewing training packages and
preparing datasets. This alpha opens packages, prepares SDXL datasets, shows
preparation progress and cancellation, and reopens saved preparation receipts.
Suite Manager handles installation, updates, shared settings and connections.

Package review shows your goal, model family, training method and image scope
before its next action. **Package details and checks** keeps the full settings,
captions, readiness evidence and optional checks accessible. LoRA, full fine-tune
and embedding are distinct configuration intents; a known method does not imply
that this alpha can execute it. Available actions follow the exact supported
target and adapter. This update adds no new training method or model support.

Dataset preparation and saved-history recovery were checked with generated
images and disposable settings. Opening a package or preparing a dataset does
not start training. Real training, trained-model quality and hardware suitability
remain separate checks; install and review compatible trainer software first.

This is a portable Windows 11 x64 alpha. Release metadata is signed, but the
Windows executables are not Authenticode-signed. Windows may show an unknown
publisher warning or block unsigned apps. Follow the
[Suite download verification guidance](https://github.com/DigitalSteel/CollectiGen#readme)
before first startup. Manager verifies module signatures and file hashes before
installing updates.

## Install and open

1. Use the complete Suite distribution with Manager **0.8.0-alpha.1** and
   launcher **0.1.1**, available from [Suite releases](https://github.com/DigitalSteel/CollectiGen/releases).
   A Manager-only update cannot upgrade an older launcher. If the matching
   complete Suite is not listed yet, keep your current installation until it is.
2. Extract the complete Suite into its own folder and start **CollectiGen.exe**.
3. In Manager, choose **Applications**, then Train's **Manage install and
   updates…**. Choose **Find Train**, review the release, and confirm Install.
4. Restart Manager if requested, then choose **Open Train** in Applications.
5. Choose **Open Training Package…** and follow its review. **Saved Preparations…**
   reopens prepared datasets; **More** keeps saved training-run history accessible.

For a supported SDXL package, choose **Prepare SDXL Dataset…**, then
**Load Saved Trainer** or expand **Trainer folders** to select an existing
installation. Review the selected folders and choose **Prepare Dataset**.
Wait for completion, then **Return to Training → Saved Preparations…**.
Select the readable preparation name and choose **Review Selected Preparation**.
**Preparation details** exposes its exact identifier and destination.
Opening a saved preparation checks its receipt, not live model/trainer readiness.

If saving the prepared copy to the list fails, keep the displayed location and
use the offered history retry when available. A retained workspace is not a
confirmed saved-list entry. Do not repeat preparation merely to retry saving it.

Keep the prepared workspace with its saved receipt. During preparation, Cancel
requests a safe stop; wait for the result before leaving. Close Train normally
when finished so Manager can safely make shared settings and resources available.

## Updates and recovery

Back up the Suite's **Data** folder and your prepared workspaces before updating.
Close the workflow and review updates through Manager. Installation and restart
remain your choices. If an installation needs recovery, use **Recover
installation** in its management window and review the proposed change.
Recovery restores the previous app state and connection when available;
saved preparations, runs, drafts and images stay separate.

## Help

- [Suite overview and usage](https://github.com/DigitalSteel/CollectiGen#readme)
- [Release notes](WHATS_NEW.md) · [GitHub release](https://github.com/DigitalSteel/CollectiGen/releases/tag/collectigen.train/v0.2.0-alpha.1)
- [Support](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/SUPPORT.md)
- [Privacy](https://github.com/DigitalSteel/CollectiGen/blob/manager-a032-source-candidate-20261011/PRIVACY.md)
