export type Target={label:string;provider:'cloudflare'|'dnspod';name:string;zone_id?:string;domain?:string;line_id?:string;token_env?:string;secret_id_env?:string;secret_key_env?:string}
export type Service={source_url:string;interval_seconds:number;timeout_seconds:number;attempts:number;dry_run:boolean;max_ips:number;pushplus_token_env:string}
export type Configuration={revision:number;service:Service;targets:Target[];credentials:{name:string;configured:boolean}[]}
export type Change={id:string;current:string;target:string;status:string;warning?:string}
export type TargetRun={label:string;provider:string;name:string;status:string;error?:string;records:Change[]}
export type Run={started_at:string;finished_at?:string;mode:string;status:string;changed:number;planned:number;targets:TargetRun[];error?:string;notification?:string}
export type Status={version:string;status:string;ips:string[];ready:boolean;dry_run:boolean;interval_seconds:number;history:Run[];targets:TargetRun[];runs:number;last_source_success?:string;last_success?:string;last_error?:string;last_started?:string;last_finished?:string;next_run_at?:string;current_run?:Run|null;pending_operations:unknown[]}
