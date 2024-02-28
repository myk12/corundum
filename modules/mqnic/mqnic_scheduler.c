// SPDX-License-Identifier: BSD-2-Clause-Views
/*
 * Copyright (c) 2021-2023 The Regents of the University of California
 */

#include "mqnic.h"

static void _mqnic_scheduler_enable(struct mqnic_sched *sched)
{
	iowrite32(1, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CTRL);
}

static void _mqnic_scheduler_disable(struct mqnic_sched *sched)
{
	iowrite32(0, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CTRL);
}

struct mqnic_sched *mqnic_create_scheduler(struct mqnic_sched_block *block,
		int index, struct mqnic_reg_block *rb)
{
	struct device *dev = block->dev;
	struct mqnic_sched *sched;
	u32 val;

	sched = kzalloc(sizeof(*sched), GFP_KERNEL);
	if (!sched)
		return ERR_PTR(-ENOMEM);

	sched->dev = dev;
	sched->interface = block->interface;
	sched->sched_block = block;

	sched->index = index;

	sched->rb = rb;

	sched->type = rb->type;
	sched->offset = ioread32(rb->regs + MQNIC_RB_SCHED_RR_REG_OFFSET);
	sched->queue_count = ioread32(rb->regs + MQNIC_RB_SCHED_RR_REG_QUEUE_COUNT);
	sched->queue_stride = ioread32(rb->regs + MQNIC_RB_SCHED_RR_REG_QUEUE_STRIDE);

	sched->hw_addr = block->interface->hw_addr + sched->offset;

	val = ioread32(rb->regs + MQNIC_RB_SCHED_RR_REG_CFG);
	sched->tc_count = val & 0xff;
	sched->port_count = (val >> 8) & 0xff;
	sched->channel_count = sched->tc_count * sched->port_count;
	sched->fc_scale = 1 << ((val >> 16) & 0xff);

	sched->enable_count = 0;

	dev_info(dev, "Scheduler type: 0x%08x", sched->type);
	dev_info(dev, "Scheduler offset: 0x%08x", sched->offset);
	dev_info(dev, "Scheduler queue count: %d", sched->queue_count);
	dev_info(dev, "Scheduler queue stride: %d", sched->queue_stride);
	dev_info(dev, "Scheduler TC count: %d", sched->tc_count);
	dev_info(dev, "Scheduler port count: %d", sched->port_count);
	dev_info(dev, "Scheduler channel count: %d", sched->channel_count);
	dev_info(dev, "Scheduler FC scale: %d", sched->fc_scale);

	_mqnic_scheduler_disable(sched);

	return sched;
}

void mqnic_destroy_scheduler(struct mqnic_sched *sched)
{
	_mqnic_scheduler_disable(sched);

	kfree(sched);
}

int mqnic_scheduler_enable(struct mqnic_sched *sched)
{
	if (sched->enable_count == 0)
		_mqnic_scheduler_enable(sched);

	sched->enable_count++;

	return 0;
}
EXPORT_SYMBOL(mqnic_scheduler_enable);

void mqnic_scheduler_disable(struct mqnic_sched *sched)
{
	sched->enable_count--;

	if (sched->enable_count == 0)
		_mqnic_scheduler_disable(sched);
}
EXPORT_SYMBOL(mqnic_scheduler_disable);

int mqnic_scheduler_channel_enable(struct mqnic_sched *sched, int ch)
{
	iowrite32(1, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_CTRL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);

	return 0;
}
EXPORT_SYMBOL(mqnic_scheduler_channel_enable);

void mqnic_scheduler_channel_disable(struct mqnic_sched *sched, int ch)
{
	iowrite32(0, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_CTRL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_disable);

void mqnic_scheduler_channel_set_dest(struct mqnic_sched *sched, int ch, int val)
{
	iowrite16(val, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC1_DEST + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_set_dest);

int mqnic_scheduler_channel_get_dest(struct mqnic_sched *sched, int ch)
{
	return ioread16(sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC1_DEST + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_get_dest);

void mqnic_scheduler_channel_set_pkt_budget(struct mqnic_sched *sched, int ch, int val)
{
	iowrite16(val, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC1_PB + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_set_pkt_budget);

int mqnic_scheduler_channel_get_pkt_budget(struct mqnic_sched *sched, int ch)
{
	return ioread16(sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC1_PB + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_get_pkt_budget);

void mqnic_scheduler_channel_set_data_budget(struct mqnic_sched *sched, int ch, int val)
{
	val = (val + sched->fc_scale-1) / sched->fc_scale;
	iowrite16(val, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC2_DB + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_set_data_budget);

int mqnic_scheduler_channel_get_data_budget(struct mqnic_sched *sched, int ch)
{
	return (int)ioread16(sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC2_DB + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE) * sched->fc_scale;
}
EXPORT_SYMBOL(mqnic_scheduler_channel_get_data_budget);

void mqnic_scheduler_channel_set_pkt_limit(struct mqnic_sched *sched, int ch, int val)
{
	iowrite16(val, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC2_PL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_set_pkt_limit);

int mqnic_scheduler_channel_get_pkt_limit(struct mqnic_sched *sched, int ch)
{
	return ioread16(sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC2_PL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_get_pkt_limit);

void mqnic_scheduler_channel_set_data_limit(struct mqnic_sched *sched, int ch, int val)
{
	val = (val + sched->fc_scale-1) / sched->fc_scale;
	iowrite32(val, sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC3_DL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE);
}
EXPORT_SYMBOL(mqnic_scheduler_channel_set_data_limit);

int mqnic_scheduler_channel_get_data_limit(struct mqnic_sched *sched, int ch)
{
	return (int)ioread32(sched->rb->regs + MQNIC_RB_SCHED_RR_REG_CH0_FC3_DL + ch*MQNIC_RB_SCHED_RR_REG_CH_STRIDE) * sched->fc_scale;
}
EXPORT_SYMBOL(mqnic_scheduler_channel_get_data_limit);

int mqnic_scheduler_queue_enable(struct mqnic_sched *sched, int queue)
{
	iowrite32(MQNIC_SCHED_RR_CMD_SET_QUEUE_ENABLE | 1, sched->hw_addr + sched->queue_stride*queue);

	return 0;
}
EXPORT_SYMBOL(mqnic_scheduler_queue_enable);

void mqnic_scheduler_queue_disable(struct mqnic_sched *sched, int queue)
{
	iowrite32(MQNIC_SCHED_RR_CMD_SET_QUEUE_ENABLE | 0, sched->hw_addr + sched->queue_stride*queue);
}
EXPORT_SYMBOL(mqnic_scheduler_queue_disable);

void mqnic_scheduler_queue_set_pause(struct mqnic_sched *sched, int queue, int val)
{
	iowrite32(MQNIC_SCHED_RR_CMD_SET_QUEUE_PAUSE | (val ? 1 : 0), sched->hw_addr + sched->queue_stride*queue);
}
EXPORT_SYMBOL(mqnic_scheduler_queue_set_pause);

int mqnic_scheduler_queue_get_pause(struct mqnic_sched *sched, int queue)
{
	return !!(ioread32(sched->hw_addr + sched->queue_stride*queue) & (1 << 7));
}
EXPORT_SYMBOL(mqnic_scheduler_queue_get_pause);
