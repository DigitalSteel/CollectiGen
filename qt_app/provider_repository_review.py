"""Plain-text presentation of bounded repository inspection, no model authority."""
import json


def inspection_text(result):
    lines = ["Selected repository inspection",
             f"Repository: {result.get('repo_id') or 'Unknown'}",
             f"Exact commit: {result.get('commit') or 'Unknown'}",
             f"Inspection: {result.get('state', 'unknown')}",
             "Runtime compatibility and total memory fit: unknown; no execution was tested.",
             "Publisher declarations, syntax verification and estimates are different evidence."]
    claims = result.get('claims', {})
    for field, label in (('model_type', 'Declared model type'), ('architectures', 'Declared architecture'),
                         ('torch_dtype', 'Declared dtype')):
        value = claims.get(field, {}).get('value')
        lines.append(label + ': ' + ('Unknown' if value is None else json.dumps(value, ensure_ascii=False)[:512]))
    weight = claims.get('weight_memory_bytes', {}).get('value')
    lines.append('Weight memory estimate: ' + (f'{weight / 1024**3:.2f} GiB (weights only)' if type(weight) is int else 'Unknown'))
    lines.append('Context, KV cache, concurrency, activations, companions and runtime overhead are excluded; this is not total fit.')
    custom = claims.get('custom_code_requirement', {}).get('value')
    lines.append('Custom code: ' + ('Declared by provider; no execution authorized.' if custom is True else 'Unknown; no code executed.'))
    labels = {"provider_declared":"Provider declared", "parser_verified":"Parser verified",
              "estimated":"Estimated", "unknown":"Unknown", "tested":"Separately tested"}
    for name, claim in result.get("claims", {}).items():
        value = claim.get("value")
        rendered = "Unknown" if value is None else json.dumps(value, ensure_ascii=False)
        lines.extend(["", name.replace("_", " ").capitalize() + ": " + rendered[:2048],
                      "Evidence: " + labels.get(claim.get("evidence_level"), "Unknown")])
        source = claim.get("source", {})
        for field in ("url", "revision", "retrieved_at", "document_sha256"):
            value = source.get(field) or (source.get('api_url') if field == 'url' else None)
            lines.append(field.replace("_", " ").capitalize() + ": " + str(value or "Unknown"))
        if claim.get("limitation"): lines.append("Limit: " + claim["limitation"])
    lines.append("\nDocuments (syntax verification does not confirm publisher claims):")
    for name, doc in result.get("documents", {}).items():
        lines.append(name + ": " + str(doc.get("state", "unknown")))
        if doc.get("validation"):
            lines.append("  Evidence: " + labels.get(doc["validation"].get("evidence_level"), "Unknown"))
        for field, value in doc.get("validation", {}).get("source", {}).items():
            lines.append("  " + field + ": " + str(value))
    lines.append("\nFiles: filename format is a hint; model bytes were not inspected.")
    lines.append("File support labels apply to the current safetensors registry route; other runtime compatibility remains untested.")
    for row in result.get("files", [])[:100]:
        lines.append(str(row.get("filename", "Unknown")) + " · registry format: " + str(row.get("registry_format_status", "unknown")))
    lines.append("\nExisting-resource suggestions: reported full hash and size matches only; fresh local verification is required.")
    for match in result.get("reuse", [])[:100]:
        lines.append(str(match.get("filename", "")) + " · " + str(match.get("resource_id", "")))
    if result.get("holds"): lines.append("\nUnavailable or held: " + ", ".join(result["holds"]))
    lines.append("\nNo weights downloaded, code executed or registry records changed by inspection.")
    lines.append("Use the original provider file review for download or evidence attachment; inspection grants neither action.")
    return "\n".join(lines)[:64000]


def confirmation_text(scope):
    return ("Contact Hugging Face for this explicit repository metadata query?\n\n"
            + scope["metadata_url"] + "\n\nRead up to five documents at the exact returned commit: "
            + ", ".join(scope["documents"]) + ".\n\n"
            + scope["pin_rule"] + "\nNo weights, credentials or model code execution. "
            "Publisher claims do not establish compatibility or runtime fit.")
