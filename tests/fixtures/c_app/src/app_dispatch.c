/************************************************************************
 * NASA Docket No. GSC-99,999-1, and identified as "Fixture App"
 *
 * Copyright (c) 2024 United States Government.
 * Licensed under the Apache License, Version 2.0 (the "License");
 ************************************************************************/

/**
 * @file
 *  Fixture App ground command dispatch
 */
#include "app_msgstruct.h"
#include "app_cmds.h"

void AppProcessGroundCommand(void *BufPtr)
{
    int cc = APP_GET_FILE_INFO_CC;

    switch (cc)
    {
        case APP_GET_FILE_INFO_CC:
            /* cross-file call, through a cast — the cFS dispatch idiom */
            AppGetFileInfoCmd((const AppGetFileInfoCmd_t *)BufPtr);
            break;

        default:
            break;
    }
}
