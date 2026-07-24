def daily_summary_cron_health(eligible_users: int, outcome_counts: dict[str, int]) -> dict[str, int]:
    """Normalize the hourly cron counters for logs and monitoring."""
    return {
        'eligible_users': eligible_users,
        'sent': outcome_counts.get('sent', 0),
        'already_sent': outcome_counts.get('already_sent', 0),
        'no_conversations': outcome_counts.get('no_conversations', 0),
        'error': outcome_counts.get('error', 0),
    }
