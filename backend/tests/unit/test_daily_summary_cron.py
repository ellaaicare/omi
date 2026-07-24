from utils.daily_summary_health import daily_summary_cron_health


def test_daily_summary_cron_health_reports_real_user_outcomes():
    result = daily_summary_cron_health(
        eligible_users=3,
        outcome_counts={'sent': 1, 'already_sent': 1, 'no_conversations': 1},
    )

    assert result == {
        'eligible_users': 3,
        'sent': 1,
        'already_sent': 1,
        'no_conversations': 1,
        'error': 0,
    }
