#ifndef APP_CMDS_H
#define APP_CMDS_H
#include "app_msgstruct.h"
void AppReportCount(unsigned int n);
void AppGetFileInfoCmd(const AppGetFileInfoCmd_t *Msg);
#endif
