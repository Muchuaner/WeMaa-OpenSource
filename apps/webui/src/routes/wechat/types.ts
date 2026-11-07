export type MaaScope = 'personal' | 'enterprise' | 'public'
export type MaaContactScope = MaaScope | 'group'
export interface MaaActivity { seq: number; id: string; kind: string; seen: number; detail: MaaFacts }
export type MaaCommandAction = 'set_polling' | 'set_group_whitelist' | 'start_watch' | 'stop_worker' | 'scan_contacts' | 'capture' | 'open_contacts' | 'open_recent' | 'friend_count' | 'checkpoint' | 'send_message' | 'send_contact_message' | 'send_media' | 'open_conversation' | 'open_contact'
export type MaaFacts = Record<string, unknown>
export interface MaaDesktopInput {
  run_id?: string; checked_at?: number; session_id?: number; process_id?: number; session_state?: string
  available?: boolean; cursor_readable?: boolean; input_desktop_accessible?: boolean
  input_desktop_name?: string; thread_desktop_name?: string; desktop_matches?: boolean
  last_error?: number | null; reason?: string; stable_checks?: number; layout_verified?: boolean; ready?: boolean
}
export interface MaaScan {
  id: string; scope: MaaScope; status: string; expected?: number; observed?: number; position_count?: number
  coverage_valid?: boolean; bottom_verified?: boolean; started?: number; finished?: number; detail?: MaaFacts
}
export interface MaaCommand {
  id: string; action: MaaCommandAction; status: string; params?: MaaFacts; detail?: unknown
  run_id?: string; created_at?: number; expires_at?: number; updated_at?: number; finished_at?: number
}
export interface MaaOverview {
  wemaa?: { ready: boolean; error?: string; identity: { identified: number; registered: number; routable: number; needs_review: number } };
  schema_version?: number; gateway_id?: string; collected_at?: number; received_at?: number; stale?: boolean; offline?: boolean
  runtime?: {
    group_whitelist?: { enabled: boolean; groups: Array<{ name: string; conversation_id: string; avatar_sha: string }>; revision: string; updated_at?: number };
    worker_state?: string; trial_state?: string; old_gateway_state?: string; run_id?: string; last_result?: unknown
    progress_updated_at?: number; progress?: MaaFacts & { run_id?: string; status?: string; desktop_input?: MaaDesktopInput }; versions?: { maafw?: string; python?: string }
    capture_asset?: string; result_delivery_warnings?: unknown[]
    sending_enabled?: boolean; auto_reply_enabled?: boolean; continuous?: boolean; execution_time_limit_seconds?: number
    layout?: { screen?: number[]; client?: number[]; divider_x?: number; dpi?: number; [key: string]: unknown }
  }
  summary?: { conversation_count?: number; message_row_count?: number; snapshot_count?: number; snapshot_status_counts?: Record<string, number> }
  scans?: MaaScan[]; events?: Array<{ id: number | string; kind: string; seen: number; detail: unknown }>
  actions?: Array<{ node: string; succeeded: boolean; elapsed_ms?: number; time_unix?: number; run_id?: string }>
  lessons?: MaaFacts; acceptance?: MaaFacts
  backups?: Array<{ name: string; sha256?: string; bytes?: number; kind?: string; checked_at?: number }>
  commands?: MaaCommand[]
  sending_enabled?: boolean; reply_backend_enabled?: boolean
}
export interface MaaReply {
  event_id: string; conversation_id: string; status: string; reply_id?: string; support_conversation_id?: string
  created_at?: number; updated_at?: number; expires_at?: number; reason?: string
}
export interface MaaReplyHistory {
  enabled: boolean; items: MaaReply[]; retention_seconds: number; sending_transport?: string
}
export interface MaaExperiment {
  retired_at?: number
  id: string; enabled: boolean; persona: string; window: string; revision: number
  mode?: string; active_count?: number; reply_wait_seconds?: number
  backup?: { sha256: string; created_at: number; version: string }
  testing_contact?: string; testing_name?: string
  acceptance?: { event_id: string; checked_at: number }
  items: Array<{ id: string; contact_id: string; name?: string; scheduled_at: number; status: string;
    pool_status?: string; awaiting_since?: number; removal_reason?: string; navigation_attempts?: number; reason?: string }>
  members: Array<{ contact_id: string; count: number; disclosed: boolean; exited: boolean; review?: { text: string }; last_inbound: number }>
}
export interface MaaContact {
  core_identity?: {id: string; person_id?: string; session_id?: string; status: string};
  id: string; scope: MaaContactScope; scan_id?: string; ordinal?: number; list_name?: string; display_name?: string; member_count?: number
  wechat_id?: string; identity_status?: string; avatar_asset?: string; profile_asset?: string; facts?: unknown; last_seen?: number
}
export interface MaaPayload {
  media_asset?: string; message_kind?: string; text?: string; sender?: string; source?: string
  direction?: string; group_sender_name?: string; group_sender_verified?: boolean
  sender_name?: string; sender_display_name?: string; sender_wechat_id?: string; sender_identity_key?: string; sender_key?: string
  sender_identity_verified?: boolean; sender_identity_status?: string
  sender_avatar_asset?: string
  sender_identity?: { display_name?: string; wechat_id?: string; key?: string; status?: string; verified?: boolean }
  visual?: { box?: number[]; verified?: boolean; avatar_box?: number[]; avatar_sha256?: string; avatar_verified?: boolean }
  [key: string]: unknown
}
export interface MaaSnapshot { id: number; status: string; seen: number; frame_asset?: string; payloads?: MaaPayload[] }
export interface MaaConversation {
  stale?: boolean;
  private_contact?: {contact_id: string; wechat_id: string; profile_verified: boolean};
  group_profile?: {name: string; kind: string; member_count: number};
  id: string; title: string; identity_status?: string; last_seen?: number; avatar_asset?: string; latest_status?: string
  unread?: boolean | null; unread_at_observation?: boolean | null; unread_observed_at?: number | null
  message_row_count?: number; latest_snapshot?: MaaSnapshot
  manual_target_observed?: boolean
  messages?: Array<{ id: string; snapshot_id: number; ordinal: number; seen: number; payload: MaaPayload }>
}
export interface MaaPage<T> { items: T[]; total: number; page: number; page_size: number; stale?: boolean }

export interface MaaScheduledMessage { kind: 'text' | 'image' | 'video' | 'greeting'; category?: 'greeting' | 'daily'; resolved_text?: string; text?: string; asset_id?: number; name?: string; status?: string; detail?: MaaFacts }
export interface MaaSchedule {
  id: string; conversation_id: string; target: { title: string; avatar_asset: string }; text: string
  recurrence?: { weekdays: number[]; time: string }; messages: MaaScheduledMessage[]; send_at: number; status: string; revision: number; command_id?: string; detail?: MaaFacts
}
