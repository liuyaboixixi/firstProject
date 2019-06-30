package com.javaproject.demo.dto;

import com.javaproject.demo.model.User;
import lombok.Data;

@Data
public class CommentDTO {
    private Long  id;
    private Long  parentId;
    private Integer  type;
    private long commentator;
    private Long gmtCreate;
    private Long gmtModified;
    private Long likeCount;
    private String content;
    private User user;
}
