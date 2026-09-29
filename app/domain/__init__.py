"""Everything the portal can do, as plain functions over a connection.

The HTML routes and the JSON API both call these, so a rule enforced here is
enforced everywhere, whichever door a request came through. Each mutating
function runs in one transaction and writes its own audit entry.

Permission checks live here, not in templates. A function either returns
what the actor may see or raises Forbidden.
"""

from .comments import (  # noqa: F401
    add_comment,
    comments,
    hide_comment,
)
from .core import (  # noqa: F401
    _CONTROL,
    _EMAIL,
    _URL,
    Actor,
    Conflict,
    DomainError,
    Forbidden,
    Invalid,
    NotFound,
    Unauthorized,
    _as_str,
    _utcnow,
    api_tokens,
    clean_email,
    clean_text,
    clean_ts,
    clean_url,
    create_api_token,
    create_user,
    get_user,
    revoke_api_token,
    user_by_email,
)
from .events import (  # noqa: F401
    DEFAULT_CRITERIA,
    EVENT_FIELDS,
    SCHEDULE_FIELDS,
    _check_schedule_change,
    _event_values,
    _flag_value,
    _save_prizes,
    create_event,
    criteria,
    get_event,
    is_organizer,
    list_events,
    lock_rubric,
    phase,
    prizes,
    prizes_of,
    require_judge,
    require_organizer,
    require_user,
    roles,
    rubric_history,
    tracks,
    update_event,
    update_rubric,
)
from .exports import (  # noqa: F401
    _cell,
    _SafeWriter,
    audit_entries,
    results_csv,
    scores_csv,
)
from .hooks import (  # noqa: F401
    create_webhook,
    delete_webhook,
    webhooks_for,
)
from .judging import (  # noqa: F401
    INVITATION_DAYS,
    _assignment_inputs,
    accept_invitation,
    assign_manual,
    auto_assign,
    get_invitation,
    invite,
    judge_queue,
    judge_scores,
    judges,
    may_claim_with,
    own_review,
    pending_invitations,
    progress,
    reviews_for,
    reviews_set_aside,
    save_review,
    set_judge_tracks,
    unassign,
)
from .pairwise_judging import (  # noqa: F401
    _direct_comparisons,
    _eligible_assigned,
    pairwise_next,
    pairwise_results,
    record_comparison,
)
from .projects import (  # noqa: F401
    PROJECT_COLUMNS,
    PROJECT_FROM,
    can_view_project,
    duplicate_candidates,
    gallery,
    get_project,
    resolve_duplicate,
    save_project,
    set_disqualified,
    unsubmit_project,
    view_project,
)
from .results import (  # noqa: F401
    compute_results,
    default_awards,
    results_for_viewer,
    track_leaders,
)
from .teams import (  # noqa: F401
    _can_join,
    create_team,
    join_team,
    leave_team,
    reset_invite,
    team_by_code,
    team_for,
    team_members,
)
from .voting import (  # noqa: F401
    _voter_check,
    abuse_signals,
    ballot,
    ballot_order_key,
    cast_vote,
    remove_votes_of,
    turnout,
    vote_reveal,
    vote_seal,
    vote_tallies,
    withdraw_vote,
)
