/************************************************************************
 * NASA Docket No. GSC-99,999-1, and identified as "Fixture App"
 *
 * Copyright (c) 2024 United States Government.
 * Licensed under the Apache License, Version 2.0 (the "License");
 ************************************************************************/

/**
 * @file
 *  Fixture App ground command handlers
 */
#include "app_msgstruct.h"
#include "app_cmds.h"

void AppReportCount(unsigned int n)
{
    (void)n;
}

void AppGetFileInfoCmd(const AppGetFileInfoCmd_t *Msg)
{
    /* same-file call: the caller and callee both live in app_cmds.c */
    AppReportCount(Msg->Count);
}
