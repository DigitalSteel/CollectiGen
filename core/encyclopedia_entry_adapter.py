"""Read-only authenticated entry metadata and retained normal-Close control.

No offer reconstruction, installation permission, channel replacement or profile
activation. The existing child control keeps the original outstanding reply ID.
"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class EncyclopediaEntryNotice:
    status: str
    reason: str
    version: str | None = None
    route: str | None = None
    notes: str = ""
    manifest_sha256: str | None = None
    package_sha256: str | None = None

    @property
    def production_install_authorized(self):
        return False


def entry_notice(value):
    from suite_core.discovery.github_component_releases import _version
    if type(value) is not dict or value.get('status') not in {'available','not_found','unavailable','held'}:
        raise ValueError('bounded_entry_notification_required')
    reason=value.get('reason')
    if type(reason) is not str or len(reason.encode('utf-8'))>4096:
        raise ValueError('bounded_entry_reason_required')
    if value['status']!='available':
        if set(value)!={'status','reason'}:
            raise ValueError('unexpected_entry_notification_fields')
        return EncyclopediaEntryNotice(value['status'],reason)
    if set(value)!={'status','reason','version','manifest_sha256','package_sha256','notes','route'}:
        raise ValueError('exact_entry_available_metadata_required')
    _version(value['version'])
    if any(type(value[k]) is not str or re.fullmatch('[0-9a-f]{64}',value[k]) is None
           for k in ('manifest_sha256','package_sha256')):
        raise ValueError('bounded_entry_hash_metadata_required')
    if value['route'] not in {'manager','manual','standalone'} or type(value['notes']) is not str or len(value['notes'].encode('utf-8'))>32768:
        raise ValueError('bounded_entry_display_metadata_required')
    return EncyclopediaEntryNotice(value['status'],reason,value['version'],value['route'],value['notes'],
        value['manifest_sha256'], value['package_sha256'])


@dataclass(frozen=True)
class EncyclopediaEntryRequest:
    status: str
    request_sha256: str


@dataclass(frozen=True)
class EncyclopediaEntryStatus:
    status: str
    phase: str | None
    current_version: str | None
    request_sha256: str | None
    reason: str = ""


def _pin(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise ValueError('exact_original_entry_request_pin_required')
    return value


class EncyclopediaEntryUpdateCheck:
    """One typed original child, shared with its host's existing serial worker."""
    def __init__(self, control):
        from suite_core.encyclopedia.control_entry import EncyclopediaChildControl
        if type(control) is not EncyclopediaChildControl:
            raise ValueError('original_child_control_required')
        self.control=control
        self.close_admitted=False
        self.retained_notice=None
        self.pending_request=None

    @property
    def close_held(self):
        return self.control.outstanding is not None

    def can_resume(self, operation, body=None):
        outstanding=self.control.outstanding
        return bool(outstanding is not None and outstanding['operation']==operation
            and outstanding['body']==(body or {}) and self.control.outstanding_sent
            and self.control.wire.channel.read_fault is None)

    def _request(self, operation, body=None):
        if self.close_admitted:
            raise ValueError('normal_close_already_admitted')
        body = {} if body is None else body
        if self.control.outstanding is not None:
            if not self.can_resume(operation, body):
                raise ValueError('original_control_operation_still_held')
            return self.control.resume_outstanding(overall_timeout=30)
        return self.control.request(operation,body,overall_timeout=30)

    def check(self, *, should_cancel=None):
        if self.close_admitted:
            raise ValueError('normal_close_already_admitted')
        if self.control.outstanding is None and should_cancel is not None and should_cancel():
            return EncyclopediaEntryNotice('held','check_cancelled_before_request')
        notice = entry_notice(self._request('check'))
        self.retained_notice = notice
        return notice

    def confirm_update(self, notice, *, confirmed=False):
        if (confirmed is not True or notice is not self.retained_notice
                or type(notice) is not EncyclopediaEntryNotice or notice.status!='available'
                or notice.route!='standalone' or self.pending_request is not None):
            raise ValueError('explicit_retained_standalone_notice_required')
        result = self._request('confirm', {'manifest_sha256': _pin(notice.manifest_sha256)})
        if type(result) is not dict or set(result)!={'status','request_sha256'} or result['status']!='waiting_for_close':
            raise ValueError('original_root_confirmation_held')
        self.pending_request = EncyclopediaEntryRequest('waiting_for_close', _pin(result['request_sha256']))
        return self.pending_request

    def status(self):
        value = self._request('status')
        if type(value) is dict and value.get('status')=='held':
            if (set(value)!={'status','reason','operation'} or value['operation']!='status'
                    or type(value['reason']) is not str or len(value['reason'].encode('utf-8'))>4096):
                raise ValueError('exact_entry_status_required')
            return EncyclopediaEntryStatus('held',None,None,
                None if self.pending_request is None else self.pending_request.request_sha256,value['reason'])
        if (type(value) is not dict or set(value)!={'status','phase','current_version','request_sha256'}
                or value['status'] not in ('idle','waiting')
                or value['phase'] not in (None,'waiting_for_close','applying','recovering')):
            raise ValueError('exact_entry_status_required')
        from suite_core.discovery.github_component_releases import _version
        _version(value['current_version'])
        pin=value['request_sha256']
        if pin is not None: _pin(pin)
        if (value['phase'] is None)!=(pin is None) or pin is not None and value['status']!='waiting':
            raise ValueError('original_entry_status_request_changed')
        if pin is None:
            self.pending_request=None
        elif self.pending_request is None or self.pending_request.request_sha256!=pin:
            self.pending_request=EncyclopediaEntryRequest('retained',pin)
        return EncyclopediaEntryStatus(value['status'],value['phase'],value['current_version'],pin)

    def _request_action(self, request, operation, allowed):
        if type(request) is not EncyclopediaEntryRequest or request is not self.pending_request:
            raise ValueError('original_retained_entry_request_required')
        result=self._request(operation,{'request_sha256':_pin(request.request_sha256)})
        if type(result) is not dict or set(result)!={'status'} or result['status'] not in allowed:
            raise ValueError('original_root_request_action_held')
        action=EncyclopediaEntryRequest(result['status'],request.request_sha256)
        if operation=='cancel': self.pending_request=None
        return action

    def cancel_update(self, request):
        return self._request_action(request,'cancel',{'cancelled_before_apply'})

    def retry_connection(self, request):
        return self._request_action(request,'retry-connection',{'saved','save_pending'})

    def recover_update(self, request, *, confirmed=False):
        if confirmed is not True: raise ValueError('explicit_original_recovery_required')
        return self._request_action(request,'recover',{'recovery_waiting_for_close'})

    def request_close(self):
        if self.close_admitted:
            return True
        if self._request('close')!={'status':'close_requested'}:
            raise ValueError('original_normal_close_acknowledgement_required')
        self.close_admitted=True
        return True
