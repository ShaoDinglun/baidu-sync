<template>
  <el-dialog
    v-model="visible"
    :title="editingTask ? '编辑任务' : '添加任务'"
    width="650px"
    :close-on-click-modal="false"
    class="add-task-dialog"
    :lock-scroll="false"
  >
    <el-form
      ref="formRef"
      :model="form"
      :rules="formRules"
      label-width="80px"
      @submit.prevent="handleSubmit"
    >
      <el-form-item label="任务名称" prop="name">
        <el-input
          v-model="form.name"
          placeholder="可选，用于标识任务"
          clearable
          @input="pathNameSync && handlePathNameSync()"
        />
      </el-form-item>
      
      <el-form-item label="转存链接" prop="url">
        <el-input
          v-model="form.url"
          placeholder="https://pan.baidu.com/s/xxx?pwd=1234 或 https://pan.baidu.com/share/init?surl=xxx&pwd=1234"
          clearable
          @blur="parseUrl"
          :loading="isParsingUrl"
        />
        <div class="form-help">支持两种格式的百度网盘链接，密码可在链接中或单独填写</div>
      </el-form-item>
      
      <el-form-item label="保存路径" prop="save_dir">
        <div class="path-input-group">
          <el-autocomplete
            v-model="form.save_dir"
            :fetch-suggestions="searchSavePaths"
            placeholder="例如：/我的资源/电影"
            clearable
            value-key="value"
            class="path-select"
          />
          <el-switch
            v-model="pathNameSync"
            class="sync-switch"
            size="small"
            :title="pathNameSync ? '已关联任务名称' : '未关联任务名称'"
            @change="handlePathNameSync"
          />
        </div>
      </el-form-item>
      
      <el-form-item label="分类" prop="category">
        <el-autocomplete
          v-model="form.category"
          :fetch-suggestions="searchCategories"
          placeholder="可选，用于任务分类"
          clearable
          value-key="value"
          style="width: 100%"
        />
      </el-form-item>

      <el-form-item label="更新方式" prop="sync_mode">
        <el-radio-group v-model="form.sync_mode">
          <el-radio label="incremental">增量更新</el-radio>
          <el-radio label="full">全量同步</el-radio>
        </el-radio-group>
        <div class="form-help">
          这里只控制订阅转存时的对比与覆盖策略，不会切换到本地 bypy 同步流程。增量更新默认只处理新增内容；全量同步会按覆盖策略重新同步目标范围。
        </div>
      </el-form-item>

      <el-form-item label="更新时间范围" prop="sync_scope_type">
        <el-radio-group v-model="form.sync_scope_type">
          <el-radio label="all">全部内容</el-radio>
          <el-radio label="recent_months">最近 N 个月</el-radio>
          <el-radio label="month_range">自定义月份区间</el-radio>
        </el-radio-group>
        <div class="form-help">
          默认按最近 2 个月识别日期目录；如果当前目录结构无法识别出日期目录，会自动回退为该任务范围内的全量处理。
        </div>
      </el-form-item>

      <el-form-item v-if="form.sync_scope_type === 'recent_months'" label="最近月数" prop="recent_months">
        <el-input-number v-model="form.recent_months" :min="1" :max="24" style="width: 220px" />
      </el-form-item>

      <el-form-item v-if="form.sync_scope_type === 'month_range'" label="月份区间" prop="scope_start_month">
        <div class="month-range-group">
          <el-date-picker
            v-model="form.scope_start_month"
            type="month"
            placeholder="开始月份"
            value-format="YYYY-MM"
            format="YYYY-MM"
          />
          <span class="range-separator">至</span>
          <el-date-picker
            v-model="form.scope_end_month"
            type="month"
            placeholder="结束月份"
            value-format="YYYY-MM"
            format="YYYY-MM"
          />
        </div>
      </el-form-item>

      <el-form-item label="覆盖范围" prop="overwrite_policy">
        <el-radio-group v-model="form.overwrite_policy">
          <el-radio label="never">不覆盖已有内容</el-radio>
          <el-radio label="window_only">仅覆盖时间范围内内容</el-radio>
          <el-radio label="always">全部允许覆盖</el-radio>
        </el-radio-group>
      </el-form-item>

      <el-form-item v-if="form.sync_scope_type !== 'all'" label="目录格式" prop="date_dir_mode">
        <div class="switch-field">
          <el-radio-group v-model="form.date_dir_mode">
            <el-radio label="auto">自动识别</el-radio>
            <el-radio label="custom">手动指定</el-radio>
          </el-radio-group>
          <div class="form-help">
            自动识别支持 YYYY-MM、YYYY/MM、YYYY年MM月、按月归档/YYYY-MM 等常见结构；如果仍然无法识别，会自动回退为全量处理。
          </div>
        </div>
      </el-form-item>

      <el-form-item v-if="form.sync_scope_type !== 'all' && form.date_dir_mode === 'custom'" label="目录模板" prop="date_dir_patterns">
        <el-select
          v-model="form.date_dir_patterns"
          multiple
          filterable
          allow-create
          default-first-option
          placeholder="选择或输入目录模板"
          style="width: 100%"
        >
          <el-option
            v-for="pattern in dateDirPatternOptions"
            :key="pattern"
            :label="pattern"
            :value="pattern"
          />
        </el-select>
        <div class="form-help">
          支持直接输入目录模板，例如 YYYY-MM、YYYY/MM、按月归档/YYYY-MM。
        </div>
      </el-form-item>

      <el-form-item label="定时规则" prop="cron">
            <div class="cron-input-group">
              <el-autocomplete
                v-model="form.cron"
                :fetch-suggestions="searchCronRules"
                placeholder="可选，使用cron表达式，例如: */5 * * * *"
                clearable
                value-key="value"
                class="cron-select"
              />
              <el-button @click="showCronHelper" size="small">cron助手</el-button>
            </div>
            <div class="form-help">留空则使用默认定时规则，例如：*/5 * * * * 表示每5分钟执行一次</div>
          </el-form-item>

          <el-form-item label="文件过滤" prop="regex_pattern">
            <el-autocomplete
              v-model="form.regex_pattern"
              :fetch-suggestions="searchRegexPatterns"
              placeholder="如：^(\d+)\.mp4$ 用于匹配需要转存的文件"
              clearable
              value-key="value"
              :disabled="form.sync_mode === 'full'"
              style="width: 100%"
            />
              <div class="form-help">正则表达式，用于匹配需要转存的文件；全量同步模式下不生效。</div>
          </el-form-item>

          <el-form-item label="文件重命名" prop="regex_replace">
            <el-autocomplete
              v-model="form.regex_replace"
              :fetch-suggestions="searchRegexReplaces"
              placeholder="如：第\1集.mp4 用于重命名文件"
              clearable
              value-key="value"
                :disabled="form.sync_mode === 'full'"
              style="width: 100%"
            />
              <div class="form-help">正则表达式替换，用于重命名文件；全量同步模式下不生效。</div>
          </el-form-item>
    </el-form>
    
    <template #footer>
      <div class="dialog-footer">
        <el-button @click="handleCancel">取消</el-button>
        <el-button
          type="primary"
          :loading="submitting"
          @click="handleSubmit"
        >
          {{ editingTask ? '更新' : '添加' }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch, reactive, computed, nextTick } from 'vue'
import { ElMessage, type FormInstance } from 'element-plus'
import { storeToRefs } from 'pinia'
import { useTaskStore } from '@/stores/tasks'
import { apiService } from '@/services'
import type {
  CreateTaskRequest,
  SubscriptionDateDirMode,
  SubscriptionOverwritePolicy,
  SubscriptionSyncMode,
  SubscriptionSyncScopeType,
  Task,
  UpdateTaskRequest
} from '@/types'

// Props
interface Props {
  modelValue: boolean
  task?: Task | null
}

interface Emits {
  (e: 'update:modelValue', value: boolean): void
  (e: 'success'): void
}

const props = withDefaults(defineProps<Props>(), {
  task: null
})

const emit = defineEmits<Emits>()

// Composables
const taskStore = useTaskStore()
const { 
  taskSavePaths, 
  taskCategories, 
  taskCronRules, 
  taskRegexPatterns, 
  taskRegexReplaces,
  getDefaultSavePath
} = storeToRefs(taskStore)

const dateDirPatternOptions = [
  'YYYY-MM',
  'YYYY-M',
  'YYYYMM',
  'YYYY年MM月',
  'YYYY/MM',
  '按月归档/YYYY-MM',
  '按月归档/YYYY年MM月'
]

interface TaskFormPayload extends CreateTaskRequest {
  name: string
  url: string
  save_dir: string
  sync_mode: SubscriptionSyncMode
  sync_scope_type: SubscriptionSyncScopeType
  recent_months: number
  scope_start_month: string
  scope_end_month: string
  overwrite_policy: SubscriptionOverwritePolicy
  date_dir_mode: SubscriptionDateDirMode
  date_dir_patterns: string[]
  category: string
  cron: string
  regex_pattern: string
  regex_replace: string
}

// 状态
const formRef = ref<FormInstance>()
const submitting = ref(false)
const pathNameSync = ref(true) // 路径和名称同步开关
const isParsingUrl = ref(false) // URL解析状态

const form = reactive<TaskFormPayload>({
  name: '',
  url: '',
  save_dir: '',
  sync_mode: 'incremental',
  sync_scope_type: 'recent_months',
  recent_months: 2,
  scope_start_month: '',
  scope_end_month: '',
  overwrite_policy: 'window_only',
  date_dir_mode: 'auto',
  date_dir_patterns: [...dateDirPatternOptions],
  category: '',
  cron: '',
  regex_pattern: '',
  regex_replace: ''
})

const buildTaskPayload = (): CreateTaskRequest & UpdateTaskRequest => ({
  name: form.name,
  url: form.url,
  save_dir: form.save_dir,
  sync_mode: form.sync_mode,
  sync_scope_type: form.sync_scope_type,
  recent_months: form.recent_months,
  scope_start_month: form.scope_start_month,
  scope_end_month: form.scope_end_month,
  overwrite_policy: form.overwrite_policy,
  date_dir_mode: form.date_dir_mode,
  date_dir_patterns: [...form.date_dir_patterns],
  category: form.category,
  cron: form.cron,
  regex_pattern: form.regex_pattern,
  regex_replace: form.regex_replace
})

// 高级设置折叠面板状态
// 移除advancedVisible，不再需要折叠面板

const formRules = {
  url: [
    { required: true, message: '请输入分享链接', trigger: 'blur' },
    {
      pattern: /^https:\/\/pan\.baidu\.com\/s\/[a-zA-Z0-9_-]+/,
      message: '请输入有效的百度网盘分享链接',
      trigger: 'blur'
    }
  ],
  save_dir: [
    { required: true, message: '请输入保存路径', trigger: 'blur' }
  ]
}

// 计算属性
const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const editingTask = computed(() => props.task)

// 方法
const resetForm = () => {
  form.name = ''
  form.url = ''
  form.save_dir = getDefaultSavePath.value || '' // 使用默认路径，如果为空则用空字符串
  form.sync_mode = 'incremental'
  form.sync_scope_type = 'recent_months'
  form.recent_months = 2
  form.scope_start_month = ''
  form.scope_end_month = ''
  form.overwrite_policy = 'window_only'
  form.date_dir_mode = 'auto'
  form.date_dir_patterns = [...dateDirPatternOptions]
  form.category = ''
  form.cron = ''
  form.regex_pattern = ''
  form.regex_replace = ''
  pathNameSync.value = true // 重置开关状态
  formRef.value?.resetFields()
}

// 解析转存链接获取文件名
const parseUrl = async () => {
  if (!form.url || isParsingUrl.value) return
  
  try {
    isParsingUrl.value = true
    
    // 解析URL和密码
    let url = form.url.split('#')[0] // 移除hash部分
    let pwd = ''
    
    if (url.includes('?pwd=')) {
      [url, pwd] = url.split('?pwd=')
    } else if (url.includes('&pwd=')) {
      [url, pwd] = url.split('&pwd=')
    }
    
    const response = await apiService.getShareInfo(url, pwd)
    
    if (response.success && response.folder_name) {
      const filename = response.folder_name
      
      // 自动填充任务名称
      if (!form.name) {
        form.name = filename
      }
      
      // 如果开启了路径和名称同步，更新保存路径
      if (pathNameSync.value && filename) {
        const basePath = getDefaultSavePath.value || ''
        // 修复路径拼接问题，避免出现//
        const cleanBasePath = basePath.replace(/\/+$/, '') // 移除末尾的所有斜杠
        form.save_dir = cleanBasePath ? `${cleanBasePath}/${filename}` : `/${filename}`
      }
    }
  } catch (error) {
    // 静默失败，不显示错误信息
    console.warn('URL解析失败:', error)
  } finally {
    isParsingUrl.value = false
  }
}

// 处理路径和名称同步
const handlePathNameSync = () => {
  if (!pathNameSync.value || !form.name) return
  
  const basePath = getDefaultSavePath.value || ''
  // 修复路径拼接问题，避免出现//
  const cleanBasePath = basePath.replace(/\/+$/, '') // 移除末尾的所有斜杠
  form.save_dir = cleanBasePath ? `${cleanBasePath}/${form.name}` : `/${form.name}`
}

// 自动补全搜索函数
const searchSavePaths = (queryString: string, callback: (suggestions: any[]) => void) => {
  const suggestions = taskSavePaths.value
    .filter(path => path.toLowerCase().includes(queryString.toLowerCase()))
    .map(path => ({ value: path }))
  callback(suggestions)
}

const searchCategories = (queryString: string, callback: (suggestions: any[]) => void) => {
  const suggestions = taskCategories.value
    .filter(category => category.toLowerCase().includes(queryString.toLowerCase()))
    .map(category => ({ value: category }))
  callback(suggestions)
}

const searchCronRules = (queryString: string, callback: (suggestions: any[]) => void) => {
  const commonCrons = [
    '*/5 * * * *',   // 每5分钟
    '0 * * * *',     // 每小时
    '0 0 * * *',     // 每天
    '0 0 * * 0',     // 每周
    '0 0 1 * *'      // 每月
  ]
  
  const allCrons = [...new Set([...commonCrons, ...taskCronRules.value])]
  const suggestions = allCrons
    .filter(cron => cron.toLowerCase().includes(queryString.toLowerCase()))
    .map(cron => ({ value: cron }))
  callback(suggestions)
}

const searchRegexPatterns = (queryString: string, callback: (suggestions: any[]) => void) => {
  const commonPatterns = [
    '^(\\d+)\\.mp4$',           // 数字命名的mp4文件
    '^.*\\.(mp4|mkv|avi)$',     // 视频文件
    '^.*\\.(jpg|png|gif)$',     // 图片文件
    '^.*\\.pdf$'                // PDF文件
  ]
  
  const allPatterns = [...new Set([...commonPatterns, ...taskRegexPatterns.value])]
  const suggestions = allPatterns
    .filter(pattern => pattern.toLowerCase().includes(queryString.toLowerCase()))
    .map(pattern => ({ value: pattern }))
  callback(suggestions)
}

const searchRegexReplaces = (queryString: string, callback: (suggestions: any[]) => void) => {
  const commonReplaces = [
    '第$1集.mp4',               // 第X集格式
    'S01E$1.mp4',              // 美剧格式
    '$1话.mp4'                 // 动漫格式
  ]
  
  const allReplaces = [...new Set([...commonReplaces, ...taskRegexReplaces.value])]
  const suggestions = allReplaces
    .filter(replace => replace.toLowerCase().includes(queryString.toLowerCase()))
    .map(replace => ({ value: replace }))
  callback(suggestions)
}

const loadTaskData = (task: Task) => {
  form.name = task.name || ''
  
  // 构建完整的转存链接（包括密码）
  let fullUrl = task.url
  if (task.pwd && task.pwd.trim() !== '' && !task.url.includes('?pwd=') && !task.url.includes('&pwd=')) {
    const separator = task.url.includes('?') ? '&' : '?'
    fullUrl = `${task.url}${separator}pwd=${task.pwd}`
  }
  form.url = fullUrl
  
  form.save_dir = task.save_dir
  form.sync_mode = task.sync_mode || 'incremental'
  form.sync_scope_type = task.sync_scope_type || 'recent_months'
  form.recent_months = task.recent_months || 2
  form.scope_start_month = task.scope_start_month || ''
  form.scope_end_month = task.scope_end_month || ''
  form.overwrite_policy = task.overwrite_policy || 'window_only'
  form.date_dir_mode = task.date_dir_mode || 'auto'
  form.date_dir_patterns = task.date_dir_patterns?.length ? [...task.date_dir_patterns] : [...dateDirPatternOptions]
  form.category = task.category || ''
  form.cron = task.cron || ''
  form.regex_pattern = task.regex_pattern || ''
  form.regex_replace = task.regex_replace || ''
  
  // 编辑任务时禁用同步开关，避免意外修改路径
  pathNameSync.value = false
  
  // 如果有高级设置数据，展开面板
  if (task.cron || task.regex_pattern || task.regex_replace) {
  }
}

// Cron助手功能
const showCronHelper = () => {
  ElMessage({
    type: 'info',
    duration: 5000,
    dangerouslyUseHTMLString: true,
    message: `
      <div style="text-align: left;">
        <strong>常用Cron表达式：</strong><br/>
        • */5 * * * * - 每5分钟<br/>
        • 0 */1 * * * - 每小时<br/>
        • 0 0 * * * - 每天0点<br/>
        • 0 0 */3 * * - 每3天0点<br/>
        • 0 0 * * 0 - 每周日0点
      </div>
    `
  })
}

const handleSubmit = async () => {
  if (!formRef.value) return

  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  if (form.sync_scope_type === 'recent_months' && form.recent_months < 1) {
    ElMessage.warning('最近月数至少为 1')
    return
  }

  if (form.sync_scope_type === 'month_range' && (!form.scope_start_month || !form.scope_end_month)) {
    ElMessage.warning('请选择完整的月份区间')
    return
  }

  if (form.sync_scope_type !== 'all' && form.date_dir_mode === 'custom' && form.date_dir_patterns.length === 0) {
    ElMessage.warning('请至少填写一个目录模板')
    return
  }

  submitting.value = true

  try {
    const payload = buildTaskPayload()

    if (editingTask.value) {
      await taskStore.updateTask(editingTask.value.order - 1, payload)
      ElMessage.success('任务已更新')
    } else {
      await taskStore.addTask(payload)
      ElMessage.success('任务已添加')
    }

    visible.value = false
    resetForm()
    emit('success')
  } catch (error) {
    ElMessage.error(`操作失败: ${error}`)
  } finally {
    submitting.value = false
  }
}

const handleCancel = () => {
  visible.value = false
  resetForm()
}

// 监听任务数据变化
watch(
  () => props.task,
  (newTask) => {
    // 使用nextTick确保DOM更新完成后再操作
    nextTick(() => {
      if (newTask) {
        loadTaskData(newTask)
      } else {
        // 强制重置表单，确保状态清空
        resetForm()
      }
    })
  },
  { immediate: true }
)

// 应用移动端样式 - 优化版本
const applyMobileStyles = () => {
  if (window.innerWidth <= 1200) { // 使用统一断点
    nextTick(() => {
      // 确保底部按钮不被地址栏遮挡
      const footer = document.querySelector('.add-task-dialog .el-dialog__footer')
      if (footer) {
        const footerElement = footer as HTMLElement
        // 使用fixed定位，确保始终在视口底部
        footerElement.style.setProperty('position', 'fixed', 'important')
        footerElement.style.setProperty('bottom', '0', 'important')
        footerElement.style.setProperty('left', '0', 'important')
        footerElement.style.setProperty('right', '0', 'important')
        footerElement.style.setProperty('z-index', '10001', 'important')
        footerElement.style.setProperty('background', 'white', 'important')
        footerElement.style.setProperty('box-shadow', '0 -2px 8px rgba(0, 0, 0, 0.1)', 'important')
        // 适配安全区域
        const safeAreaBottom = getComputedStyle(document.documentElement).getPropertyValue('env(safe-area-inset-bottom)') || '20px'
        footerElement.style.setProperty('padding-bottom', `calc(16px + ${safeAreaBottom})`, 'important')
      }
      
      // 调整内容区域，为底部按钮留出空间
      const body = document.querySelector('.add-task-dialog .el-dialog__body')
      if (body) {
        const bodyElement = body as HTMLElement
        bodyElement.style.setProperty('padding-bottom', '120px', 'important') // 更多底部间距
        bodyElement.style.setProperty('height', 'calc(100vh - 160px)', 'important')
      }
    })
  }
}

// 监听对话框显示状态
watch(visible, (newVisible) => {
  if (!newVisible) {
    resetForm()
  } else {
    // 对话框打开时，如果没有编辑任务，重置表单
    if (!props.task) {
      // 使用nextTick确保DOM更新完成后再重置
      nextTick(() => {
        resetForm()
      })
    }
    // 应用移动端样式
    applyMobileStyles()
  }
})

// 监听任务名称变化，自动同步到保存路径
watch(() => form.name, (newName) => {
  if (pathNameSync.value && newName) {
    handlePathNameSync()
  }
})

// 监听同步开关变化
watch(pathNameSync, (newValue) => {
  if (newValue && form.name) {
    handlePathNameSync()
  }
})

watch(() => form.sync_mode, (mode) => {
  if (mode === 'full' && form.overwrite_policy === 'window_only' && form.sync_scope_type === 'all') {
    form.overwrite_policy = 'always'
  }

  if (mode === 'full') {
    form.regex_pattern = ''
    form.regex_replace = ''
  }
})

watch(() => form.sync_scope_type, (scopeType) => {
  if (scopeType === 'all') {
    form.date_dir_mode = 'auto'
    form.scope_start_month = ''
    form.scope_end_month = ''
    if (form.sync_mode === 'full' && form.overwrite_policy === 'window_only') {
      form.overwrite_policy = 'always'
    }
  }
})

watch(() => form.date_dir_mode, (mode) => {
  if (mode === 'auto') {
    form.date_dir_patterns = [...dateDirPatternOptions]
  } else if (form.date_dir_patterns.length === 0) {
    form.date_dir_patterns = [...dateDirPatternOptions]
  }
})
</script>

<style scoped>
.add-task-dialog :deep(.el-dialog__header) {
  padding: 20px 20px 10px;
  border-bottom: 1px solid #e4e7ed;
}

.add-task-dialog :deep(.el-dialog__body) {
  padding: 20px;
}

.add-task-dialog :deep(.el-dialog__footer) {
  padding: 10px 20px 20px;
  border-top: 1px solid #e4e7ed;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}

.el-form-item {
  margin-bottom: 20px;
}

.el-form-item :deep(.el-form-item__label) {
  font-weight: 500;
  color: #606266;
}

.switch-field {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.el-textarea :deep(.el-textarea__inner) {
  resize: vertical;
  min-height: 80px;
}

/* 高级设置区域样式 */
.advanced-settings {
  margin-top: 20px;
}

.form-help {
  font-size: 12px;
  color: #909399;
  margin-top: 5px;
  line-height: 1.4;
}

.path-input-group {
  display: flex;
  align-items: center;
  gap: 12px;
}

.path-select {
  flex: 1;
}

.cron-input-group {
  display: flex;
  align-items: center;
  gap: 12px;
}

.cron-select {
  flex: 1;
}

.month-range-group {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.range-separator {
  color: #909399;
  flex-shrink: 0;
}

.sync-switch {
  flex-shrink: 0;
}

.sync-switch :deep(.el-switch__core) {
  background-color: #dcdfe6;
}

.sync-switch :deep(.is-checked .el-switch__core) {
  background-color: #409eff;
}

/* 确保对话框在移动端有正确的层级 */
:deep(.el-overlay) {
  z-index: 9999 !important;
  background-color: rgba(0, 0, 0, 0.5) !important;
}

.add-task-dialog {
  z-index: 10000 !important;
}

/* 响应式调整 - 统一断点为1200px */
@media (max-width: 1200px) {
  :deep(.el-overlay) {
    padding: 0 !important;
  }
  
  /* 直接覆盖Element Plus的dialog样式 */
  :deep(.el-dialog.add-task-dialog) {
    width: 100vw !important;
    max-width: 100vw !important;
    margin: 0 !important;
    border-radius: 0 !important;
    height: 100vh !important;
    max-height: 100vh !important;
    z-index: 10000 !important;
    overflow: hidden !important;
    position: fixed !important;
    top: 0 !important;
    left: 0 !important;
    right: 0 !important;
    bottom: 0 !important;
  }
  
  :deep(.el-dialog.add-task-dialog .el-dialog__header) {
    padding: 16px 20px 10px 20px !important;
    border-bottom: 1px solid #f0f0f0;
  }
  
  :deep(.el-dialog.add-task-dialog .el-dialog__body) {
    padding: 16px 20px 100px 20px !important; /* 增加底部padding为按钮和安全区域留空间 */
    height: calc(100vh - 140px) !important; /* 减少高度为按钮留更多空间 */
    overflow-y: auto !important;
    box-sizing: border-box !important;
  }
  
  :deep(.el-dialog.add-task-dialog .el-dialog__footer) {
    padding: 16px 20px !important;
    padding-bottom: calc(16px + env(safe-area-inset-bottom, 20px)) !important; /* 适配安全区域 */
    border-top: 1px solid #f0f0f0;
    position: fixed !important; /* 使用fixed确保始终在视口底部 */
    bottom: 0 !important;
    left: 0 !important;
    right: 0 !important;
    background: white !important;
    display: flex !important;
    justify-content: flex-end !important;
    align-items: center !important;
    gap: 12px !important;
    z-index: 10001 !important; /* 确保在最上层 */
    box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.1) !important; /* 添加阴影效果 */
    min-height: 70px !important; /* 确保按钮区域有足够高度 */
  }
  
  /* 表单标签调整 */
  :deep(.el-dialog.add-task-dialog .el-form-item__label) {
    width: 80px !important;
    font-size: 14px !important;
    line-height: 1.4 !important;
  }
  
  /* 表单内容区域 */
  :deep(.el-dialog.add-task-dialog .el-form-item__content) {
    margin-left: 80px !important;
  }
  
  /* 移动端优化表单按钮 */
  :deep(.el-dialog.add-task-dialog .el-button) {
    min-height: 44px !important;
    padding: 12px 24px !important;
    border-radius: 8px !important;
    font-size: 16px !important;
    font-weight: 500 !important;
    min-width: 80px !important;
  }
  
  /* 底部按钮特别样式 */
  :deep(.el-dialog.add-task-dialog .el-dialog__footer .el-button) {
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1) !important;
  }
  
  /* 取消按钮样式 */
  :deep(.el-dialog.add-task-dialog .el-dialog__footer .el-button--default) {
    background: #f5f7fa !important;
    border-color: #e4e7ed !important;
    color: #606266 !important;
  }
  
  /* 主要操作按钮样式 */
  :deep(.el-dialog.add-task-dialog .el-dialog__footer .el-button--primary) {
    background: #409eff !important;
    border-color: #409eff !important;
    color: white !important;
  }
  
  /* 移动端优化表单输入框 */
  :deep(.el-dialog.add-task-dialog .el-input),
  :deep(.el-dialog.add-task-dialog .el-textarea) {
    width: 100% !important;
  }
  
  :deep(.el-dialog.add-task-dialog .el-input__inner),
  :deep(.el-dialog.add-task-dialog .el-textarea__inner) {
    min-height: 44px !important;
    font-size: 16px !important; /* 防止iOS放大 */
    width: 100% !important;
    box-sizing: border-box !important;
    padding: 12px !important;
    border-radius: 6px !important;
    word-break: break-all !important;
    overflow-wrap: break-word !important;
  }
  
  /* 移动端表单项优化 */
  :deep(.el-dialog.add-task-dialog .el-form-item) {
    margin-bottom: 20px !important;
  }
  
  /* 帮助文本优化 */
  :deep(.el-dialog.add-task-dialog) .form-help {
    font-size: 12px !important;
    line-height: 1.4 !important;
    margin-top: 6px !important;
  }
  
  /* 高级设置按钮 */
  :deep(.el-dialog.add-task-dialog .advanced-toggle) {
    width: 100% !important;
    margin-top: 8px !important;
  }
}
</style>
