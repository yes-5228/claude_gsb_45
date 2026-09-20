import Tag from './Tag.jsx'

/** 自动汇总日均值的完整性标注: 有效小时数不足时提示数据不完整. */
export default function CompletenessTag({ isComplete, validHours }) {
  if (isComplete === null || isComplete === undefined || isComplete) return null
  return (
    <Tag tone="warning" title={`有效小时仅 ${validHours ?? '-'} 个, 未达到日均值完整性要求`}>
      数据不完整
    </Tag>
  )
}
