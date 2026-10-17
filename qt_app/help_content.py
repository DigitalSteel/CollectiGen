"""Canonical workspace guides. Coordinate changes through docs/HELP_MAINTENANCE.md."""

from types import MappingProxyType


WORKSPACE_HELP = MappingProxyType({
    "setup": """## Start here

Use Setup for storage locations, appearance, optional local AI, and application maintenance.

Everyday automation rules live in **Rules & Automation**. Optional components are installed only after you review and confirm them.

## Find the right defaults

In **Getting Started → Workflow defaults**, choose **Preparation Defaults…** for evidence intake and preparation, **AI Behavior Defaults…** for analysis after library checks, or **Analysis Models and Descriptions…** for local analysis configuration.

**Generation Setup…** opens image and video connection settings. SwarmUI handles image generation; **Open Video Setup…** opens the separate local ComfyUI video setup. **Training Setup…** manages trainer software and folders. Choose training presets in Training and generation recipes or output presets in Generate; applying one changes the reviewed plan, not existing packages or other saved plans.

## Model locations

In **AI Configuration → Model Locations**, choose **New model folders**, **Fast storage**, or **Analysis model folder**. New model folders and Fast storage share **Save Model Settings**; analysis has its separate **Save Analysis Model Folder**. Switching tasks preserves pending choices without saving them.

Folder preferences describe where new acquisitions go and optional verified Fast copies are prepared. **Browse or Index Models…** can register existing files in place; saving folder preferences does not move models. Indexing, organization, Fast preparation and cleanup each have their own review.

## Optional SwarmUI companion

In SwarmUI setup, **Companion Package…** prepares a separate folder for the optional read-only identity companion. New preparation needs a saved connection and the exact retained developer candidate. Choose a new folder, **Preview Package**, then **Create Package**. This does not install or activate the companion. **Existing Package → Reopen Package…** lets you **Verify Package** or **Review Recovery**, even while disconnected or when the original candidate is unavailable. Recovery is read-only; changed, unknown or unowned files stay in place.

## Saving and recovery

In SwarmUI **Configuration Backups → Saved Backups**, select a copy and choose **Review Recovery Coverage** to check its archived settings and see what remains outside full installation recovery. This works offline. **Prepare Recovery Files…** separately reviews copies into a new private folder; the files may contain credentials. It never replaces the installed configuration.

## History storage

In **Libraries & Storage → History & Versions**, choose a source and explicitly designate private, machine-local history storage. **Save History Locations** saves that group's configuration; **Discard Changes** discards only that group's draft. Saving locations does not inspect or move source files. Disabled and retired stores remain protected. A changed source folder or privacy role requires a fresh storage review; changing storage starts a separate store and leaves earlier history in place. These settings prepare storage; they do not by themselves enable capture in every workflow.

## Save preferences and analysis configuration

Everyday preferences save immediately. If a save fails, follow the displayed recovery state: the previous preference is restored when safe, while a separately changed file is retained for review. AI model and tuning edits remain staged until **Save AI Configuration**. Reusable policies have their own Save action in Rules & Automation.

Model Memory settings concern image analysis. Training and generation recheck their own resources for each plan. Installing a resource or seeing a connection does not establish that an exact request will fit in memory.""",

    "dashboard": """## Start here

Dashboard shows library health and work that needs attention. Use the sidebar to continue your work.

- **Library status** opens or repairs the current library view.
- **Guided workflows** starts a library-wide review.
- **Dataset** owns image selection and training collections; **Training** prepares packages, runs, and evaluates results.
- **Generate** owns image and video drafts, jobs, and results. Verified outputs return to **Ingest** unreviewed, with their lineage.
- **SwarmUI connection** reports the last observed image-generator state, not Training or video readiness. Check the exact model and request in its workspace before running.
- **Rules & Automation** controls reusable behavior.
- **Setup** manages storage and application requirements.

## How the workspaces fit together

- **Ingest → Dataset:** Review incoming images, then add them to a library. Select images and manage training collections in Dataset.
- **Training:** Prepare a collection, review its exact training setup, then run and evaluate. Reopen saved work in Run History and Results.
- **Generate → Ingest:** Create or resume image and video drafts in Generate. Review verified results in Ingest before using them again.

Enter at the stage you need. Generation does not require training a model first. Generated outputs keep their lineage and remain unreviewed until you review them.

## Optional Tag review and recovery

**Review Tag Names…** appears when a library check finds new SDXL Tag names without saved vocabulary decisions. Choose whether to remember a name, use an existing Tag, ignore it, or decide later. Undecided names may appear again.

**View Tag Recovery…** appears when a previous image Tag update has saved recovery information. Open it to inspect the changes and available recovery options. Its presence does not mean a new update is waiting to be applied.""",

    "ingest": """## Start here

Add images or scan the Ingest folder, then select the images you want to prepare.

## Typical workflow

1. Review the image summary and any findings.
2. Edit or prepare the selected images when needed.
3. Move finished images to a library, then use Dataset to organize collections and prepare training data.

Generated images and extracted video frames return here with their lineage for review. After adding images, **Review Added Images** opens the exact returned images even when the gallery has other filters. Generate owns image creation; Training owns saved training plans and runs.

Importing external evidence reads adjacent TXT and Metadata only after you review the exact scope. Moving images uses the previewed destination and recovery checks.

If Prepare Images needs an AI model, **Open Setup** leads to Components. Choose and set up a model there, then use **Return to Preparation** to review the same images and one-run choices again. Returning does not download a model or start image work; **Start Preparation** remains a separate decision.

**Run AI…** works only on the images you explicitly select. If it opens AI model setup, your image and option choices stay in the Run AI review; returning from setup starts nothing. Check the selection and choose **Run AI** again when ready. A fresh image-scope and saved-history check runs before work starts.

## Images and videos

Use **View → Review Media…** to inspect a library or subfolder by media type or attention needed. Images and registered clips keep separate review paths; the dialog's Help explains inspection limits. Unsupported animations, renamed clips and unexpected files remain in place. The review does not confirm image facts or add video to training.""",

    "dataset": """## Start here

Search your libraries, select an image to inspect it, or select several images for a focused review.

## Choose your next goal

Use **Training → Training Collections** in this page's ribbon to create and manage collections. Select Dataset images and choose **Add to Collection…** in the same ribbon or image Actions menu. **Build Training Data…** continues with the collection's exact images in the dedicated Training section. **Open Training** returns to saved plans, resources and runs. Each plan freezes its image list; later collection edits leave saved plans unchanged.

**Open Generate** continues creation in the dedicated Generate section. **Use Image Settings…** carries selected images' recorded settings and reviewed knowledge into a draft. To change the image itself or animate it, use **Remix This Image…** or **Animate This Image…** in its Actions menu. Verified results return to Ingest for review.

Use the ribbon's **Organize** group for maintenance and recovery, **Import** for external evidence, and **Training** for collections, saved training plans and caption rules. **Find Commands** (Ctrl+K) locates commands and settings.

## Review training captions

Choose **Training → Review Caption Rules…** to see which confirmed Tags automatic rules combine or omit. The review keeps the selected image list, or current matching result, fixed. **Preview Automatic Rule Effects** changes no image facts or files; rules requiring manual review do not run here.

**Read Problems** names images that could not be checked. Restore file access or use **Edit Image Data…** for that exact image, then **Retry Preview** checks the original scope again. **Customize Filters…** changes only displayed effects; hidden effects are not missing facts. **Back to Dataset** retains selection.

**Review Exact Training TXT Export…** is the separate final caption review. Its selected profile also controls descriptions, ordering and caption strategy; the automatic-rule diagnostic is not the final TXT. Review the exact export before writing files.

## Prepare selected images

If Prepare Images needs an AI model, **Open Setup** shows its Components choice. **Return to Preparation** reopens the same image scope and one-run choices for a fresh review; it does not start a download or preparation. A changed selection requires a new review.

In full Prepare Images, the checked **Save preparation and fingerprint choices as defaults** option saves those choices when you choose **Start Preparation**. Uncheck it for a one-run change. Preview and Cancel save no defaults; saving defaults does not mean the image work completed.

**Run AI…** uses your explicit selection and keeps it when AI model setup is needed. Returning from setup does not start a run. Review the images and options, then choose **Run AI** again; current storage and saved history are checked before work starts.

**View → Review Media…** inspects a library or subfolder without changing the current image selection. Images and registered clips keep separate review paths. Open the dialog's local Help for inspection limits and unsupported media.""",

    "review": """## Start here

Select a question, review the examples, and save the answer that matches your intent.

Generated results return to Ingest unreviewed. Inspect their image facts there or
in Image Data; generator prompts are evidence, not confirmed facts. Review Center
collects knowledge questions, not a list of all unreviewed images.

Training uses selected images and versioned caption profiles. A completed Review
queue does not establish training readiness. Review exact captions in Training.

**Dismiss Question** removes an item from the active queue without changing image data or reusable knowledge. You can restore dismissed questions later.

## Queue updates

Questions refresh when saved Image Knowledge or reviewed rules change. **Recheck Questions…** uses saved information and does not reread TXT or Metadata. **Reload Queue** reads saved questions and outcomes; unchanged questions keep your selection and unfinished answer. A changed question may require a new decision.

**Clear Filters** resets question type, queue quality and search while retaining the selected Show status. **Dismiss Queue Noise…** previews pending noise before dismissal; completed and dismissed history are not new questions. If saving fails, the previous queue state remains available with a retry explanation. Missing related images need to be made available before image-dependent work can continue.""",

    "automation": """## Choose where to work

- **Rules** starts with a goal choice for training captions, incoming-Tag screening, conflict checks or automatic behavior. **Browse All Rules…** opens the full inventory for precise editing.
- **Reusable Tags** manages reviewed templates, legacy compatibility and Knowledge Packs.
- Choose **Prepare Images**, **After Library Checks**, or another section for its defaults.
- Use **Find a setting** to jump to a control without saving pending edits.

## Rule behavior

- **Merge rules** shape exported training captions.
- **Ignore rules** screen exact incoming evidence without removing saved facts.
- **Conflict rules** flag possible issues for review; they do not change images.

Older unclassified rules keep their current behavior until you review them.

## Review and save defaults

**AI Analysis Defaults** offers **Review first**, **Analyze automatically**, and **Analyze and confirm safe results** starting points. Preview compares the current draft with the proposed preset. **Stage Preset Changes** changes the draft only; it does not run analysis.

**Save AI / Prepare Defaults** saves AI defaults, Tag suggestions, after-check behavior and Prepare defaults together. **Discard Pending Changes** restores that saved scope while preserving other policy drafts. Editing a control invalidates an old preset preview. A failed save keeps the draft available for correction and retry.

## Training and generation

These AI defaults analyze existing images. Training presets and generation recipes stay with their reviewed plans; automation rules never start those jobs or change frozen training packages. Generated results return to Ingest unreviewed with their lineage.

Setup manages local models, hardware, and storage. Dataset owns caption review for the selected images. Training owns packages and runs; Generate owns image/video drafts and results. The page that starts an operation lets you review its exact scope and result.""",

    "knowledge": """## Start here

Search for a Tag or category to review its meaning, relationships, sources, and organization.

Category edits save automatically; Tag Details uses **Save Tag**. Linked Character Knowledge saves automatically within its editor. This page never changes image files, captions, Metadata, or raw evidence.

## Find and reuse a Tag

Enter a preferred Tag name or alias in search and press Enter to open one exact match within the current Review scope. Partial, ambiguous or excluded matches remain browsing decisions. Closing Tag Details retains your query, category and selection. Clear the search or choose **Review → All tags** to broaden an empty result.

Use the category's **Actions** menu or a Tag's context menu to copy selected preferred names, one per line. Copying does not edit Tags, images or prompts. If a saved edit cannot refresh the workspace, **Retry Workspace Update** retries the display refresh; the visible save result remains authoritative.

## Use vocabulary in your workflow

- **Images:** confirm facts in the image editor. A reusable Tag or relationship is not proof that it appears in an image.
- **Training:** choose collections in Dataset and prepare training in Training. Caption profiles select from confirmed Image Knowledge without deleting it; category caption order is a reusable default, not model-specific training readiness.
- **Generate:** deliberately choose Tags in the prompt editor, or copy selected names from the category's **Actions** menu. Prompt text and model trigger suggestions do not confirm image facts. Generated results return through Ingest for review.

Reusable matching and automation rules open in **Rules & Automation**.""",

    "generate": """## Start here

Choose **Image** or **Video**, then start or resume a draft. For a source image, use **Remix This Image…** or **Animate This Image…** in Ingest or Dataset. **Use Image Settings…** transfers settings and reviewed knowledge; it does not by itself condition generation on the source pixels.

For a managed saved image request, choose **Resume Image Draft…** before opening a workspace. Search or page through drafts, then select one; an empty list offers **Start New Image…**. Cancel or a changed saved file leaves current work untouched. **Open Draft…** inside an image workspace remains the route for a separately chosen file. Resuming never starts generation or skips current model and connection checks.

## Video drafts and next steps

For a saved video request, choose **Video → Open Video Draft…** and select its file before a new workspace opens. A draft with newer or unsupported fields stays available for inspection and saving, but cannot be submitted through controls that do not represent those fields. Video runtime and model readiness still need fresh review.

In the video workspace, the current next step stays visible. **More Video Actions…** shows the other stage controls without starting work. Active cancellation, recovery, Stop Runtime, and verified-result actions appear when they apply. If model preparation needs recovery, use **Review Model Recovery…** to reach its retained control.

## Reusable generation setups

**Create Generation Setup…** lets you write reusable prompts and settings manually, or choose a known Character or Outfit and review recurring details from its tagged images. Choose the exact identity and included libraries; suggestions are unchecked until you select them. Save the setup for later or choose to use it now. **Use Saved Setup…** on Generate lets you choose a named setup before opening a separate new image draft, review its changes, and deliberately apply them. Inside an existing request, **Apply Saved Setup** remains available for a reviewed merge. Missing Character or Outfit records are managed in Tag Encyclopedia.

## Import SwarmUI presets

For a local SwarmUI preset export, open **Saved Setups → Create from SwarmUI Preset… → Source Presets → Choose SwarmUI File…**.

Choose one preset, or open **Combine Presets (optional)** to combine up to eight. Use **Add Chosen Preset**, arrange their order, and choose the source for each conflicting setting. **Use Reviewed Combination in Setup** continues with supported literal text and settings; review the editable setup before saving.

**Model Matches** can explicitly match a contributing main-model or VAE reference to an exact registered copy. Source LoRA lists, dynamic syntax and unsupported fields remain in the omission review, not executable selections. After saving, **Review Import Details…** shows retained source details even without the original export file.

**Export Selected…** opens a format and content review. Prompt text is excluded unless you check **Include prompt text in the exported file**.

CollectiGen export retains reusable setup choices without local import details. SwarmUI export supports literal text and basic settings only; setups with model components, Remix or finishing controls must use CollectiGen export.

Choose a new file, then import it in SwarmUI separately and review its models and settings before generation.

## Review the request

A Remix source panel offers whole-image changes, painted-area edits and generative upscale. Switch drafts without losing edits. Review the exact model, prompt, output settings and local connection before submitting. Image generation uses SwarmUI; video needs its own exact ComfyUI runtime and model setup.

For image requests, **Lower Memory** reduces the base canvas without replacing an already small draft with a larger model default. **Preview Recommended Plan** shows the base and desired final size; **Use Recommended Settings** applies the reviewed changes. Faithful resizing reaches the reviewed final target, but fine detail can differ and memory fit is not guaranteed. Observed timing and retained-file size describe matching completed requests; missing measurements are not estimates.

For supported new SDXL images, **Place Regional Prompts…** opens up to two nonoverlapping rectangular prompt areas. Its local Help explains limitations and canvas changes. Alternatives can compare two settings in a grid of up to 32 sequential candidates; review the grid before launching work.

## Check image memory readiness

Readiness shows the **last observed** free GPU memory. The reading expires after 30 seconds; free memory does not prove that the exact request fits. If a known memory limit blocks the request, resolve it and choose **Check Reviewed Request** again. Waiting for the reading to expire does not clear that block. Resources are checked again when queued work starts.

SwarmUI's **Review Installation…** can show a read-only identity snapshot from an optional companion when separately installed and available. If it is absent or unavailable, stock connection and generation remain available. Successful compilation alone does not mean the companion is installed or qualified against a running server; its identity report does not prove memory fit or authorize runtime control.

Setup's **Companion Package…** offers explicit detached package creation and later verification or read-only recovery review. It requires the reviewed developer candidate for new creation; normal SwarmUI loading and installation remain separate.

## Results and recovery

Successfully verified images and videos return through Ingest unreviewed with their lineage. Review image facts before reuse; a prompt is generator evidence, not a confirmed description. A failed or memory-blocked request retains its draft and recovery information. Review the indicated changes and request checks before deliberately retrying; changing a preset does not automatically rerun a failed job.""",

    "training": """## Start here

Use the three stages to prepare selected data, run a reviewed training package and evaluate the result. Training is optional; you can generate images with an existing supported model.

## 1. Prepare

Choose **Dataset Collections** or **Saved Training Drafts**. **Manage Collections in Dataset…** opens collection membership; **Prepare Training…** or **Resume Draft…** rechecks the saved item before continuing. Review the exact images, goal, model, caption profile and settings. A saved plan freezes its image list; changing a collection does not rewrite an existing plan or package.

If a saved draft's package preview is cancelled or fails before writing, **Retry Package Preview…** appears beside the Training status. It retains that draft and destination choice but checks the saved draft, source images and destination again before showing a new review. You still choose **Build** separately. If the draft changed, choose **Refresh** and **Resume Draft…** instead; interrupted publication uses **Recover Interrupted Package…**, not this preview retry.

For an interrupted package, choose **Recover Interrupted Package…**, then the parent folder where the package would be saved. Select the named saved package to inspect its exact receipt without changing files. **Browse Exact Receipt…** is available if you know the receipt file. If files are staged but publication needs the original plan, choose **Find Related Saved Drafts…**. These are possible source drafts, not proof of a match: select one, review its current plan and a fresh preview for the held destination, then deliberately choose Recover. The exact receipt, sources and full plan are rechecked; a changed plan or receipt leaves the stage held.

Caption profiles select useful training text from confirmed Image Knowledge without deleting other valid facts. A complete Review Center queue does not prove that captions or the selected data are ready for training.

For extracted video frames, review the source-video split groups. Frames sharing a source video stay together rather than appearing in both training and evaluation; use other source videos for independent evaluation. Invalid or stale lineage needs review or re-extraction before inclusion. This does not add video training support.

Preview training presets and workload before applying settings. Preset application is staged until the configuration is saved. Smaller batches or lower-memory settings do not prove a run will fit. **Save external configuration** records an external plan; it does not make unsupported settings executable by the local trainer.

## 2. Run

Review the exact package, trainer resources and readiness before starting. Opening a draft or installing software does not start training. Use **Trainer Setup…** for software and **Models…** for registered resources. Registration alone does not establish model compatibility.

**Open Training Package…** verifies a published package. **Recover Interrupted Package…** reviews interrupted preparation; it does not recover automatically. Use the available safe cancellation action and wait for any deliberate recovery to finish. Resolve an uncertain start through its recovery route before attempting another run.

## 3. Evaluate

Use **Run History and Results…** for saved runs, **Experiments and Compare Runs…** for comparisons, and **Compare Generated Samples…** for image evaluation. Preparing missing samples has a separate generation launch review. Save or discard unfinished notes when prompted. Generated samples return through Ingest for review; measured speed and memory observations do not prove output quality.""",

})


def workspace_help(topic_id: str) -> str:
    """Return an exact guide; unknown IDs fail rather than showing unrelated help."""
    return WORKSPACE_HELP[topic_id]


SECTION_HELP = MappingProxyType({
    "models": """## Choose a model for your goal

Add an existing model file or preview a folder, then register the files you want to use. Registration records identity and location; it does not move files or prove that a model works with every trainer or generator. Review the declared role and family, then check compatibility for the exact plan.

## Review known model folders

Use **Local Models → Review Known Folders…** to review saved Setup destinations and previously indexed folders with their recorded sources. Opening the list does not scan folders or connect to a server. **Read Connected SwarmUI Folders** optionally reads the enrolled server's folder settings.

Choose one folder and **Preview Selected Folder**, review its files and declared role/family, then **Register Previewed Files**. Folder hints do not establish compatibility. **Back to Model Selection** restores your earlier selection and pending notes. Unsupported paths need **Browse Another Folder…** or **Setup Folders…**; changed settings need **Reload Models** or a fresh connected read. **Stop Read** cancels the settings read without registering files.

## Import models from an existing SwarmUI installation

Connect the installation in **Setup → AI Tools & Models → Generate & Remix** first. In Models, choose **Import Models from SwarmUI…**, then **Read Source / Folders** and **Choose SwarmUI Folder…**. Select one recorded folder and preview its files before assigning each checked file a model type (what it does) and family (its architecture or version). The combined plan registers only reviewed files; it does not move them by default. **Choose Files…** can add another exact local path without treating folder names as model identity.

Open **Optional: organize files or prepare Fast copies** only if needed. Choose Images or Video for each import choice, and use **Folder Locations…** when a saved destination is missing. **Review Combined Plan** shows exact source and destination effects before **Apply Reviewed Plan**; changing folder preferences requires a fresh review. Fast copies and sharing model folders with SwarmUI are separate optional effects, not part of ordinary registration.

## Reconnect an exact model copy

Select a registered single-file model and choose **Locate Exact Copy…** from its details or Models **Locations & Lineage**. Choose one `.safetensors` file. CollectiGen checks its complete identity before offering **Add Verified Location**, then checks the file and registry again when you apply.

A different model cannot replace the selected identity through this action. Existing locations, declarations, notes and lineage stay recorded; files are not moved. Return to the original selection and use the ordinary **Verify** or **Use** action before continuing. If the registry changes, close and reload before retrying; interrupted publication offers **Recover Registry**. Managed model packs keep their existing pack-registration route.

## Inspect recorded lineage

In **Models → Local Models → Locations & Lineage**, select a registered model. Choose **Ordered Parents**, **Derived Children**, **Operations and Evidence**, **Typed References**, or **Recorded Locations**. **Raw Details…** shows the complete recorded fields in pages, including fields the summary does not interpret.

**Inspect Related Model** follows a parent or child only when its exact identity is already registered. **Back to Previous Model** restores your search, selection and relationship view; pending notes stay with their model. Unknown references remain visible. Supported training-package references use the explicit route below; other typed references remain display-only. Inspection does not search libraries, open inferred paths, revalidate recorded locations or execution receipts, or establish compatibility.

## Inspect a referenced training package

In **Locations & Lineage → Typed References**, select the recorded package fingerprint and choose **Choose Referenced Package…**. Explicitly choose its public package folder. CollectiGen verifies the package and its recorded identity before opening read-only **Frozen Data** and **Frozen Settings**. These show frozen image identities, decisions, split roles, captions and settings—not thumbnails or a restored training plan. Original libraries and execution receipts are not revalidated; no folder is discovered automatically from a hash.

**Back to Models** preserves your search, selection and pending notes. A wrong, missing, changed or protected package cannot open as a verified result. Use **Stop Work** to cancel a check safely, then choose the folder again to retry. Each inspection needs a fresh folder choice and verification. Ambiguous manual dataset or training-manifest hashes remain unavailable for navigation.

## Keep or recover pending notes

In **Notes**, use **Save Model Notes** to save the selected model's tags and notes. **Review Pending Notes…** lets you inspect and copy pending edits, including edits for removed models. **Discard Pending Notes** clears only the selected model's edits after confirmation.

If the registry changes or becomes unavailable, navigation pauses until **Reload**. Older pending notes cannot overwrite newer saved notes; copy them for comparison or discard them to load saved values. If a related model disappears on reload, you return to the original search. Drafts are retained only while this Models window stays open, not across crashes or restarts. Closing or accepting with pending edits requires an explicit discard decision.

## Optional provider evidence

Use cached evidence when available. Online lookups use the public query you review and require the displayed network choice. Provider descriptions and family suggestions are evidence, not a substitute for local file verification or an executable adapter.

Review the exact file, destination and download terms before confirming acquisition. A preview does not download. Incomplete downloads remain unavailable; use the offered recovery path rather than registering partial files. Changing storage defaults does not move existing resources.

## Continue your work

Choose a model in Training or Generate for that plan's resource and compatibility checks. Planning guidance depends on the exact method, adapter and machine evidence. Installed files, benchmark speed or available GPU memory do not prove request fit or output quality.

In planning guidance, **Sources & Updates** previews optional local knowledge updates before you apply them. If an update is interrupted or saved knowledge changes, choose **Recover / Reload Knowledge**. It checks the retained evidence and reloads a proven saved result; uncertain or conflicting files stay untouched. Your expert choices remain, but you must build a fresh plan before using it.""",
    "review-media": """## Inspect a library

Choose a library or an original subfolder, then use All media, Images, Videos or Needs attention. Inspection reads file headers to distinguish still images, animations, videos and unexpected files. It is not a full decode or proof that every file is healthy. Results are shown in pages of up to 100 rows.

## Follow the relevant review path

Images and videos keep separate review workflows. Registered clips use their recorded temporal lineage and protected playback. Unregistered clips, unexpected extensions and animated files remain in place; inspection does not convert them, register external video, confirm Image Knowledge or add video to training.

Still-image intake rejects renamed clips and animated inputs instead of silently treating them as ordinary training images. Choose the correct supported media route. Close this review to return to the unchanged image selection. Resolve unavailable or redirected folders before retrying the inspection.""",
    "generation-regions": """## Place a prompt in part of the image

Add up to two nonoverlapping rectangles on the current generation canvas and write a prompt for each area. These are rectangular prompt areas, not semantic masks, independent subjects or style controls. The global prompt remains exactly as written; the global/region blend is fixed at 0.5.

This option supports new SDXL images through the managed Swarm Comfy route. Other request combinations must pass their own supported-mode checks; placing rectangles does not bypass them.

## Review and apply

Check the numeric bounds, canvas sketch and exact prompt preview. **Apply Regional Prompts** updates the current draft; it does not launch generation. **Cancel** leaves the original draft unchanged. Review and submit through the normal Generate workflow.

## When the canvas changes

Old rectangles need a fresh review after a recipe or output setting changes the canvas. **Use Rectangles on Current Canvas** confirms their use on the new canvas after validation; **Remove All Regions** clears them. Overlapping or invalid bounds must be corrected before applying. If a saved specification cannot be read, follow the rebuild or clear route in Generate rather than silently discarding it.""",
})


def section_help(topic_id: str) -> str:
    """Return guidance for a dialog with its own local Help entry."""
    return SECTION_HELP[topic_id]
