from typing import Dict, Any, List
import copy
from datetime import datetime
from urllib.parse import unquote

def clean_auto_workflow_response(response: Dict[str, Any], keyword: str = None) -> Dict[str, Any]:
    """
    Clean and optimize the AutoWorkflowResponse for AI Agent consumption.
    Removes debug info, diagnostics, and redundant fields.
    """
    # Create a deep copy to avoid modifying the original object if it's used elsewhere
    cleaned = copy.deepcopy(response)

    # 0. Inject keyword if provided (User request)
    if keyword:
        cleaned["keyword"] = keyword
        # Also inject into note_result as requested by user (with null check)
        if "note_result" in cleaned and cleaned["note_result"] is not None:
            cleaned["note_result"]["keyword_used"] = keyword

    # 1. Remove top-level diagnostics
    if "diagnostics" in cleaned:
        del cleaned["diagnostics"]

    # 2. Simplify login_status
    if "login_status" in cleaned and cleaned["login_status"]:
        login_status = cleaned["login_status"]
        cleaned["login_status"] = {
            "success": login_status.get("logged_in", False),
            "message": "Login successful" if login_status.get("logged_in") else "Login failed",
            # Keep nickname if available as a sanity check for the user
            "nickname": login_status.get("nickname")
        }

    # 3. Clean search_result
    if "search_result" in cleaned and cleaned["search_result"]:
        search_res = cleaned["search_result"]
        # Remove diagnostics from search result
        if "diagnostics" in search_res:
            del search_res["diagnostics"]
        
        # Fix URL encoding in page_url (Make it human readable)
        if "page_url" in search_res and search_res["page_url"]:
            url = search_res["page_url"]
            # Unquote up to 3 times to handle double/triple encoding
            for _ in range(3):
                if not url or "%" not in url:
                    break
                new_url = unquote(url)
                if new_url == url:
                    break
                url = new_url
            search_res["page_url"] = url

    # 4. Clean note_result (The most important part)
    if "note_result" in cleaned and cleaned["note_result"]:
        note_result = cleaned["note_result"]
        
        # Remove diagnostics
        if "diagnostics" in note_result:
            del note_result["diagnostics"]
            
        # Clean each note
        if "notes" in note_result:
            cleaned_notes = []
            for note in note_result["notes"]:
                cleaned_note = _clean_single_note(note)
                
                # Remove hot_comments_summary as requested
                if "hot_comments_summary" in cleaned_note:
                    del cleaned_note["hot_comments_summary"]
                    
                cleaned_notes.append(cleaned_note)
            note_result["notes"] = cleaned_notes
            
            # Add collected_count for easier Agent processing
            note_result["collected_count"] = len(cleaned_notes)

    # 5. Recursively remove empty fields (null, "", []) to save tokens
    cleaned = _remove_empty_fields(cleaned)

    return cleaned


def _remove_empty_fields(obj: Any) -> Any:
    """Recursively remove empty fields (None, "", [], {}) from dicts and lists."""
    if isinstance(obj, dict):
        return {
            k: v
            for k, v in ((k, _remove_empty_fields(v)) for k, v in obj.items())
            if v not in (None, "", [], {})
        }
    elif isinstance(obj, list):
        return [
            v
            for v in (map(_remove_empty_fields, obj))
            if v not in (None, "", [], {})
        ]
    else:
        return obj


def _clean_single_note(note: Dict[str, Any]) -> Dict[str, Any]:
    """Clean a single note dictionary."""
    # Remove any potential debug fields that might have slipped in
    keys_to_remove = ["debug_html", "raw_data", "internal_id"]
    for key in keys_to_remove:
        if key in note:
            del note[key]

    # Ensure captured_at is a string (ISO format)
    if "captured_at" in note:
        val = note["captured_at"]
        if isinstance(val, datetime):
            note["captured_at"] = val.isoformat()

    # Clean comments
    if "comments" in note:
        cleaned_comments = []
        for comment in note["comments"]:
            cleaned_comment = _clean_comment(comment)
            cleaned_comments.append(cleaned_comment)
        note["comments"] = cleaned_comments

    return note


def _clean_comment(comment: Dict[str, Any]) -> Dict[str, Any]:
    """Clean a single comment dictionary."""
    # Keep sub_comments but clean them recursively
    if "sub_comments" in comment:
        cleaned_subs = []
        for sub in comment["sub_comments"]:
            cleaned_subs.append(_clean_comment(sub))
        comment["sub_comments"] = cleaned_subs
    
    # Remove parent_id if it's None or empty
    if "parent_id" in comment and not comment["parent_id"]:
        del comment["parent_id"]

    # Remove create_time as it is often 0 and not useful
    if "create_time" in comment:
        del comment["create_time"]

    return comment
