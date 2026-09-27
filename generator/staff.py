"""
Staff-only pages. Linked from the account menu for staff accounts only.
"""
import datetime

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.db.models import Count, Sum
from django.shortcuts import render
from django.utils import timezone

from .ai_client import _tokens_key
from .models import AIResult, AIUsageDay, Generation

USAGE_DAYS = 30


def _cost(model, input_tokens, output_tokens):
    """Estimated USD, or None for a model with no entry in AI_PRICES_PER_MTOK."""
    price = getattr(settings, 'AI_PRICES_PER_MTOK', {}).get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


@staff_member_required
def ai_usage(request):
    """What the AI costs: tokens and estimated spend by day and model."""
    today = timezone.now().date()
    since = today - datetime.timedelta(days=USAGE_DAYS - 1)

    rows = list(AIUsageDay.objects.filter(date__gte=since))
    unpriced = set()
    for row in rows:
        row.cost = _cost(row.model, row.input_tokens, row.output_tokens)
        if row.cost is None:
            unpriced.add(row.model)

    def spend(selected):
        return sum(r.cost or 0 for r in selected)

    today_rows = [r for r in rows if r.date == today]
    budget = getattr(settings, 'AI_DAILY_TOKEN_BUDGET', 0)
    # The budget is enforced from the cache counter, so show that number.
    today_tokens = cache.get(_tokens_key()) or sum(r.input_tokens + r.output_tokens for r in today_rows)

    by_model = (AIUsageDay.objects.filter(date__gte=since).values('model')
                .annotate(calls=Sum('calls'), input_tokens=Sum('input_tokens'),
                          output_tokens=Sum('output_tokens')).order_by('-calls'))
    by_model = list(by_model)
    for m in by_model:
        m['cost'] = _cost(m['model'], m['input_tokens'], m['output_tokens'])

    # Heaviest accounts this calendar month, by saved results.
    month_start = today.replace(day=1)
    counts = {}
    for qs in (Generation.objects, AIResult.objects):
        for row in (qs.filter(created_at__date__gte=month_start)
                    .values('user__username', 'user__profile__plan').annotate(n=Count('id'))):
            key = (row['user__username'], row['user__profile__plan'])
            counts[key] = counts.get(key, 0) + row['n']
    top_accounts = sorted(counts.items(), key=lambda kv: -kv[1])[:10]

    return render(request, 'generator/staff_ai_usage.html', {
        'rows': rows,
        'by_model': by_model,
        'days': USAGE_DAYS,
        'today_calls': sum(r.calls for r in today_rows),
        'today_tokens': today_tokens,
        'today_cost': spend(today_rows),
        'period_cost': spend(rows),
        'budget': budget,
        'budget_pct': round(100 * today_tokens / budget) if budget else None,
        'unpriced': sorted(unpriced),
        'top_accounts': [{'username': u, 'plan': p or 'free', 'results': n} for (u, p), n in top_accounts],
        'month_start': month_start,
    })
