/************************************************************************
 * NASA Docket No. GSC-99,999-1, and identified as "Fixture App"
 *
 * Copyright (c) 2024 United States Government.
 * Licensed under the Apache License, Version 2.0 (the "License");
 ************************************************************************/

/**
 * @file
 *  Fixture App command message structures
 */
#ifndef APP_MSGSTRUCT_H
#define APP_MSGSTRUCT_H

#define APP_GET_FILE_INFO_CC 5

typedef struct
{
    unsigned int Count;
} AppGetFileInfoCmd_t;

typedef unsigned int AppId_t;

#endif
