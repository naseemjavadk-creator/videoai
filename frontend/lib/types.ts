export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Clip {
  index: number;
  title: string;
  description: string;
  hashtags: string[];
  viral_score: number;
  reason: string;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  transcript: string;
  video_url: string;
  download_url: string;
  thumbnail_url: string;
  expires_at: string;
}

export interface Job {
  job_id: string;
  status: JobStatus;
  stage: string;
  progress: number;
  message: string;
  error?: string | null;
  created_at: string;
  updated_at: string;
  url: string;
  source_title?: string | null;
  source_channel?: string | null;
  video_summary?: string | null;
  clips: Clip[];
}

export interface JobSummary {
  job_id: string;
  status: JobStatus;
  stage: string;
  progress: number;
  message: string;
  created_at: string;
  url: string;
  source_title?: string | null;
  clip_count: number;
}

export interface CreateJobInput {
  url: string;
  max_clips: number;
  min_clip_seconds: number;
  max_clip_seconds: number;
  language_code: string;
  tts_voice?: string;
  commentary_style: string;
  caption_color: string;
  rights_confirmed: boolean;
}

export interface YouTubeStatus {
  configured: boolean;
  connected: boolean;
  channelTitle?: string;
  channelId?: string;
}

export type PrivacyStatus = "private" | "unlisted" | "public";

export interface UploadInput {
  jobId: string;
  index: number;
  title: string;
  description: string;
  tags: string[];
  privacyStatus: PrivacyStatus;
  madeForKids: boolean;
  containsSyntheticMedia: boolean;
}

export interface UploadResult {
  videoId: string;
  url: string;
}
