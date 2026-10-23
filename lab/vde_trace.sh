#!/bin/sh
# Read-only kernel timing trace of the Tegra VDE decode path (run with sudo).
# Records ~10 s of function_graph durations for a fixed list of driver/DMA
# functions while the display stream is running, then restores tracing state.
set -eu
SECONDS_TO_TRACE=${1:-10}
T=/sys/kernel/tracing
OUT=/home/user/vde-trace-$(date +%Y%m%d-%H%M%S)
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ -w "$T/current_tracer" ] || mount -t tracefs nodev "$T"

restore() {
	echo 0 > "$T/tracing_on"
	echo nop > "$T/current_tracer"
	echo > "$T/set_ftrace_filter"
	echo 0 > "$T/events/enable"
	echo > "$T/trace"
	echo 1 > "$T/tracing_on"
}
trap restore EXIT INT TERM

FUNCS="tegra_device_run tegra_vde_h264_setup_frame tegra_vde_h264_decode_run
tegra_vde_h264_decode_wait tegra_vde_wait_mbe tegra_vde_wait_bsev tegra_vde_isr
tegra_decode_complete tegra_vde_prepare_control_data
tegra_vde_dmabuf_cache_map tegra_vde_dmabuf_cache_unmap tegra_vde_iommu_map
tegra_vde_iommu_unmap tegra_vde_runtime_resume tegra_vde_runtime_suspend
tegra_buf_prepare tegra_buf_queue tegra_buf_request_complete
vb2_dma_sg_prepare vb2_dma_sg_finish vb2_dma_sg_map_dmabuf vb2_dma_sg_unmap_dmabuf
__dma_sync_sg_for_device __dma_sync_sg_for_cpu iommu_map_sg iommu_unmap
reset_control_assert reset_control_deassert reset_control_reset v4l2_m2m_job_finish"

echo 0 > "$T/tracing_on"
echo > "$T/trace"
echo 8192 > "$T/buffer_size_kb"
echo function_graph > "$T/current_tracer"
for f in $FUNCS; do echo "$f" >> "$T/set_ftrace_filter" 2>/dev/null || echo "skip $f" >&2; done
echo funcgraph-abstime > "$T/trace_options"
echo funcgraph-proc > "$T/trace_options"
echo 1 > "$T/events/dma/dma_sync_sg_for_device/enable" 2>/dev/null || true
echo 1 > "$T/events/dma/dma_sync_sg_for_cpu/enable" 2>/dev/null || true
grep -E '54200000.dc|vde' /proc/interrupts > "$OUT.irq-before"
echo 1 > "$T/tracing_on"
sleep "$SECONDS_TO_TRACE"
echo 0 > "$T/tracing_on"
grep -E '54200000.dc|vde' /proc/interrupts > "$OUT.irq-after"
cat "$T/trace" > "$OUT.txt"
cat "$T/per_cpu/cpu"*/stats > "$OUT.stats" 2>/dev/null || true
chown user:user "$OUT".*
echo "trace written: $OUT.txt ($(wc -l < "$OUT.txt") lines)"
