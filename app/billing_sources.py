"""Read-only provider billing adapters. Never forward credentials to report downloads."""
import csv
import io
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlsplit

import httpx
import modal


class BillingUnavailable(Exception):
    pass


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise BillingUnavailable('Provider returned an invalid cost.') from None
    if not result.is_finite() or abs(result) > Decimal('1000000000'):
        raise BillingUnavailable('Provider returned an invalid cost.')
    return result


def midnight(day):
    return datetime.combine(day, datetime.min.time(), timezone.utc)


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


async def modal_costs(settings, start, end):
    """Daily, pre-credit costs for an exact app ID plus explicitly allocated objects."""
    client = await modal.Client.from_credentials.aio(settings.modal_token_id, settings.modal_token_secret)
    app = await modal.App.lookup.aio(settings.modal_app_name, create_if_missing=False, client=client)
    objects = {app.app_id, *settings.modal_billing_object_ids.split(',')} - {''}
    workspace = modal.Workspace.from_context(client=client)
    rows = await workspace.billing.report.aio(start=midnight(start), end=midnight(end + timedelta(days=1)), resolution='d')
    days = {}
    for row in rows:
        if row.object_id not in objects:
            continue
        day = row.interval_start.astimezone(timezone.utc).date()
        if start <= day <= end:
            days[day.isoformat()] = days.get(day.isoformat(), Decimal(0)) + amount(row.cost)
    return days


def temporal_csv(content, namespace, start, end):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    required = {'ResourceID', 'BillingCurrency', 'ContractedCost', 'ChargePeriodStart', 'ChargePeriodEnd'}
    if not required.issubset(reader.fieldnames or []):
        raise BillingUnavailable('Temporal returned an unsupported billing report format.')
    days = {}
    for row in reader:
        if row['ResourceID'] != namespace:
            continue
        # Temporal documents ContractedCost in USD cents, including fractional cents.
        if row['BillingCurrency'] not in {'USD', 'USD (cents)'}:
            raise BillingUnavailable('Only USD billing reports are supported.')
        try:
            lower = datetime.fromisoformat(row['ChargePeriodStart'].replace('Z', '+00:00'))
            upper = datetime.fromisoformat(row['ChargePeriodEnd'].replace('Z', '+00:00'))
        except (ValueError, TypeError):
            raise BillingUnavailable('Temporal returned an invalid charge period.') from None
        if lower.tzinfo is None or upper.tzinfo is None or lower != midnight(lower.date()) or upper - lower != timedelta(days=1):
            raise BillingUnavailable('Temporal must return daily UTC charge periods.')
        day = lower.date()
        if start <= day <= end:
            days[day.isoformat()] = days.get(day.isoformat(), Decimal(0)) + amount(row['ContractedCost']) / 100
    return days


async def temporal_costs(settings, start, end, state, save_state):
    """Resume one durable report request; None means generation is still pending."""
    base = 'https://saas-api.tmprl.cloud/cloud/billing-reports'
    headers = {'Authorization': 'Bearer ' + settings.temporal_billing_api_key,
               'temporal-cloud-api-version': 'v0.23.0'}
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        if not state.get('report_id'):
            response = await client.post(base, headers=headers, json={
                'asyncOperationId': state['operation_id'],
                'spec': {'startTimeInclusive': midnight(start.replace(day=1)).isoformat(),
                         'endTimeExclusive': midnight(next_month(end)).isoformat(),
                         'granularity': 'BILLING_REPORT_GRANULARITY_DAILY',
                         'description': 'Moyai infrastructure cost tracking'},
            })
            response.raise_for_status()
            state['report_id'] = response.json()['billingReportId']
            save_state(state)
        response = await client.get(base + '/' + quote(state['report_id'], safe=''), headers=headers)
        # A successful CreateBillingReport can precede visibility in GetBillingReport.
        # Keep the saved ID and let the worker poll within its existing timeout.
        if response.status_code == 404:
            return None
        response.raise_for_status()
        report = response.json()['billingReport']
        if report['state'] == 'BILLING_REPORT_STATE_IN_PROGRESS':
            return None
        if report['state'] != 'BILLING_REPORT_STATE_GENERATED':
            raise BillingUnavailable('Temporal could not generate the billing report. Retry sync.')
        downloads = report.get('downloadInfo', [])
        if not downloads:
            raise BillingUnavailable('Temporal returned no billing report files.')
        days, size = {}, 0
        for item in downloads:
            parsed = urlsplit(item['url'])
            # Only documented provider-controlled report hosts; no arbitrary/internal fetches.
            allowed = parsed.hostname and (parsed.hostname.endswith('.amazonaws.com') or parsed.hostname.endswith('.tmprl.cloud'))
            if parsed.scheme != 'https' or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise BillingUnavailable('Temporal returned an unsupported report download host.')
            if item.get('fileFormat') != 'FILE_FORMAT_CSV':
                raise BillingUnavailable('Temporal returned a report that is not CSV.')
            chunks = []
            async with client.stream('GET', item['url']) as download:
                download.raise_for_status()
                async for chunk in download.aiter_bytes():
                    size += len(chunk)
                    if size > 20 * 1024 * 1024:
                        raise BillingUnavailable('Temporal billing report exceeds the 20 MB limit. Choose fewer dates.')
                    chunks.append(chunk)
            for day, cost in temporal_csv(b''.join(chunks).decode('utf-8-sig'), settings.temporal_namespace, start, end).items():
                days[day] = days.get(day, Decimal(0)) + cost
        return days
